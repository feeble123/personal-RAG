"""Embedding 服务：OpenAI 兼容 API（默认硅基流动免费 BGE-M3）。

- 接口抽象 EmbeddingProvider → 切换厂商/本地模型零代码改动
- 查询向量 LRU 缓存（降延迟）
- 文档向量按 content_hash + profile_fingerprint 走 DB 缓存（省 API 调用）

P1-3：EmbeddingProfile 指纹——模型切换/instruction/维度变化时，缓存 key 改变，
同一 content_hash 不同配置分开存，永不因配置漂移而错配。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass
from functools import lru_cache
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import BizError
from app.db.models import EmbeddingCache

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EmbeddingProfile:
    """P1-3：embedding 配置指纹。任何字段变化 → 不同 fingerprint → 缓存/索引隔离。"""

    provider: str
    model: str
    base_url: str
    dimension: int
    normalize: bool
    query_instruction: str
    doc_instruction: str
    tokenizer: str

    def fingerprint(self) -> str:
        """稳定指纹（sha256 前 32 位）。"""
        raw = json.dumps(
            {
                "provider": self.provider,
                "model": self.model,
                "base_url": self.base_url,
                "dimension": self.dimension,
                "normalize": self.normalize,
                "query_instruction": self.query_instruction,
                "doc_instruction": self.doc_instruction,
                "tokenizer": self.tokenizer,
            },
            sort_keys=True,
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _build_profile(dim: int | None = None) -> EmbeddingProfile:
    """从 settings 构建当前 profile。dim 未知时由 embedder 探测。"""
    return EmbeddingProfile(
        provider=settings.embedding_provider,
        model=settings.embedding_model,
        base_url=settings.embedding_base_url,
        dimension=dim if dim is not None else 0,
        normalize=False,
        query_instruction=settings.embedding_query_instruction or "",
        doc_instruction="",  # 当前无 doc instruction 配置
        tokenizer="tiktoken-cl100k" if settings.embedding_model.startswith("BAAI") else "unknown",
    )


_current_profile: EmbeddingProfile | None = None


def get_profile() -> EmbeddingProfile:
    """当前 profile（lazy：首次探测维度）。"""
    global _current_profile
    if _current_profile is None:
        _current_profile = _build_profile(_probe_dimension())
    return _current_profile


def profile_fingerprint() -> str:
    return get_profile().fingerprint()


def reset_profile() -> None:
    """测试用：重置 profile 缓存。"""
    global _current_profile
    _current_profile = None


def _probe_dimension() -> int:
    """探测当前 embedder 的向量维度（固定 probe 文本）。"""
    try:
        v = get_embedder().embed_query("probe")
        return len(v)
    except Exception:
        return 0


class EmbeddingProvider(Protocol):
    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...
    def embed_query(self, text: str) -> list[float]: ...


class OpenAICompatibleEmbedding:
    """OpenAI 兼容嵌入客户端（硅基流动 / OpenAI / 任意兼容厂商）。"""

    def __init__(self) -> None:
        if not settings.embedding_api_key:
            raise BizError("未配置 EMBEDDING_API_KEY（硅基流动 https://siliconflow.cn 免费注册获取）", 500, "EMBEDDING_NOT_CONFIGURED")
        from langchain_openai import OpenAIEmbeddings  # 懒加载

        self.model = settings.embedding_model
        self._client = OpenAIEmbeddings(
            model=self.model,
            api_key=settings.embedding_api_key,
            base_url=settings.embedding_base_url,
            max_retries=3,
        )
        logger.info("Embedding 客户端就绪: %s @ %s", self.model, settings.embedding_base_url)

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self._client.embed_documents(texts)

    def embed_query(self, text: str) -> list[float]:
        if settings.embedding_query_instruction:
            text = settings.embedding_query_instruction + text
        return self._client.embed_query(text)


class FakeEmbedding:
    """离线测试/开发模式：确定性哈希向量，无需 API Key。

    `EMBEDDING_PROVIDER=fake` 时启用，用于验证整条 RAG 管线（无需网络）。
    """

    def __init__(self, dim: int = 64) -> None:
        self.model = "fake-hash"
        self._dim = dim

    def _vec(self, text: str) -> list[float]:
        import hashlib

        h = hashlib.sha256(text.encode("utf-8")).digest()
        return [h[i % len(h)] / 255.0 for i in range(self._dim)]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._vec(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        if settings.embedding_query_instruction:
            text = settings.embedding_query_instruction + text
        return self._vec(text)


_embedder: EmbeddingProvider | None = None


def get_embedder() -> EmbeddingProvider:
    global _embedder
    if _embedder is None:
        if settings.embedding_provider == "fake":
            _embedder = FakeEmbedding()
            logger.warning("使用 FAKE Embedding（离线测试模式），检索质量为演示级别")
        else:
            _embedder = OpenAICompatibleEmbedding()
    return _embedder


def reset_embedder() -> None:
    """测试用：重置单例。"""
    global _embedder
    _embedder = None


# ---- 查询向量 LRU 缓存（进程级，key 含 profile 指纹）----
@lru_cache(maxsize=4096)
def _cached_query_vector(profile: str, model: str, text: str) -> tuple[float, ...]:
    v = get_embedder().embed_query(text)
    return tuple(v)


async def embed_query(text: str) -> list[float]:
    """异步查询向量：LRU 缓存（key 含 profile 指纹）+ 线程池。"""
    vector = await asyncio.to_thread(
        _cached_query_vector, profile_fingerprint(), settings.embedding_model, text
    )
    return list(vector)


# ---- 文档向量（批量 + DB 缓存，key 含 profile 指纹）----
def _embed_docs_sync(texts: list[str]) -> list[list[float]]:
    return get_embedder().embed_documents(texts)


async def embed_documents(texts: list[str]) -> list[list[float]]:
    """批量文档向量（不进 DB 缓存的纯计算路径）。"""
    vectors: list[list[float]] = []
    for i in range(0, len(texts), settings.embedding_batch_size):
        batch = texts[i : i + settings.embedding_batch_size]
        vectors.extend(await asyncio.to_thread(_embed_docs_sync, batch))
    return vectors


async def load_cache_vectors(db: AsyncSession, hashes: list[str]) -> dict[str, list[float]]:
    """从 embedding_cache 表加载已计算向量（按 profile 指纹精确匹配）。"""
    if not settings.embedding_cache_enabled or not hashes:
        return {}
    fp = profile_fingerprint()
    rows = await db.scalars(
        select(EmbeddingCache).where(
            EmbeddingCache.content_hash.in_(hashes),
            EmbeddingCache.profile_fingerprint == fp,
        )
    )
    return {r.content_hash: json.loads(r.vector_json) for r in rows}


async def store_cache_vectors(db: AsyncSession, vectors: dict[str, list[float]]) -> None:
    """写入 embedding_cache 表（同 content_hash+profile 已存在则跳过）。"""
    if not settings.embedding_cache_enabled or not vectors:
        return
    fp = profile_fingerprint()
    existing = await db.scalars(
        select(EmbeddingCache.content_hash).where(
            EmbeddingCache.content_hash.in_(list(vectors)),
            EmbeddingCache.profile_fingerprint == fp,
        )
    )
    exist_set = set(existing)
    for h, v in vectors.items():
        if h in exist_set:
            continue
        db.add(
            EmbeddingCache(
                content_hash=h,
                profile_fingerprint=fp,
                model_version=settings.embedding_model,
                vector_json=json.dumps(v),
            )
        )
    await db.commit()
