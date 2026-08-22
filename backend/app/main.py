"""应用入口：lifespan（建表/种子 admin/预热）、中间件、路由注册、静态托管。"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from app.core.config import settings
from app.core.exceptions import register_exception_handlers
from app.core.ratelimit import limiter
from app.db.session import async_session_factory, ensure_db_at_head, init_db

logging.basicConfig(
    level=logging.DEBUG if settings.debug else logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s | %(message)s",
)
logger = logging.getLogger("app")


async def _seed_admin() -> None:
    """首次启动创建管理员 admin/123456。"""
    from sqlalchemy import select

    from app.core.security import hash_password
    from app.db.models import User

    async with async_session_factory() as db:
        admin = await db.scalar(select(User).where(User.username == settings.admin_username))
        if not admin:
            db.add(
                User(
                    username=settings.admin_username,
                    password_hash=hash_password(settings.admin_password),
                    nickname="系统管理员",
                    role="admin",
                )
            )
            await db.commit()
            logger.info("已创建管理员账号: %s", settings.admin_username)


async def _warmup_bm25() -> None:
    """启动预热：从 DB 重建各知识库 BM25 语料，避免重启后首问慢/无 BM25 结果。"""
    try:
        from sqlalchemy import select

        from app.db.models import Chunk
        from app.services import bm25
        # jieba 默认 DEBUG 级并自带无格式 stderr handler，启动时刷屏「分词词典加载」日志；
        # 提到 WARNING 让启动日志干净。注意必须在 import 之后设置（jieba import 时强制回 DEBUG）。
        logging.getLogger("jieba").setLevel(logging.WARNING)

        async with async_session_factory() as db:
            rows = await db.execute(
                select(Chunk.kb_id, Chunk.id, Chunk.content).order_by(Chunk.id)
            )
            grouped: dict[int, list[tuple[int, str]]] = {}
            for kb_id, cid, content in rows:
                grouped.setdefault(kb_id, []).append((cid, content))
        for kb_id, items in grouped.items():
            import asyncio

            await asyncio.to_thread(bm25.rebuild, kb_id, items)
        logger.info("BM25 预热完成: %d 个知识库", len(grouped))
    except Exception:
        logger.exception("BM25 预热失败（不影响启动）")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 准备数据目录
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.upload_dir_path.mkdir(parents=True, exist_ok=True)
    settings.chroma_dir_path.mkdir(parents=True, exist_ok=True)
    # 建表 + 种子 + 预热 + 清空语义缓存（防旧检索答案残留劫持新检索）
    await init_db()
    # P0-6：检查迁移版本是否 head（只检查不自动迁移）
    ensure_db_at_head()
    await _seed_admin()
    await _warmup_bm25()
    from app.services import semantic_cache

    await semantic_cache.clear_cache()
    logger.info("应用启动完成: %s", settings.app_name)
    yield
    logger.info("应用关闭")


def create_app() -> FastAPI:
    app = FastAPI(
        title=settings.app_name,
        version="1.0.0",
        lifespan=lifespan,
        docs_url="/api/docs" if settings.debug else None,
        openapi_url="/api/openapi.json" if settings.debug else None,
    )

    # 限流异常处理
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    app.add_middleware(SlowAPIMiddleware)

    # 响应压缩
    app.add_middleware(GZipMiddleware, minimum_size=1000)

    # CORS
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # 统一异常处理
    register_exception_handlers(app)

    # 路由
    from app.modules.auth.routes import router as auth_router
    from app.modules.conversations.routes import router as conversations_router
    from app.modules.knowledge.routes import public_router as kb_public_router
    from app.modules.knowledge.routes import router as knowledge_router
    from app.modules.qa.routes import router as qa_router
    from app.modules.users.routes import router as users_router
    from app.modules.users.routes import stats_router
    from app.modules.memory.routes import kb_memories_router as kb_memories_router
    from app.modules.memory.routes import router as memory_router

    api_prefix = "/api"
    app.include_router(auth_router, prefix=api_prefix)
    app.include_router(conversations_router, prefix=api_prefix)
    app.include_router(knowledge_router, prefix=api_prefix)
    app.include_router(kb_public_router, prefix=api_prefix)
    app.include_router(qa_router, prefix=api_prefix)
    app.include_router(users_router, prefix=api_prefix)
    app.include_router(stats_router, prefix=api_prefix)
    app.include_router(memory_router, prefix=api_prefix)
    app.include_router(kb_memories_router, prefix=api_prefix)

    # 健康检查
    @app.get("/api/health")
    async def health() -> JSONResponse:
        return JSONResponse({"status": "ok", "app": settings.app_name})

    # 静态资源（前端构建产物；开发时前端跑 Vite dev server 走 CORS/proxy）
    frontend_dist = Path(__file__).resolve().parents[2] / "frontend" / "dist"
    if frontend_dist.exists():
        from fastapi.responses import FileResponse

        # SPA fallback：未匹配 API 的路径返回 index.html
        assets = frontend_dist / "assets"
        if assets.exists():
            app.mount("/assets", StaticFiles(directory=assets), name="assets")

        async def _serve_index():
            return FileResponse(frontend_dist / "index.html")

        @app.get("/{full_path:path}", include_in_schema=False)
        async def spa_fallback(full_path: str):
            if full_path.startswith("api/"):
                return JSONResponse({"code": "NOT_FOUND", "message": "接口不存在"}, status_code=404)
            return await _serve_index()
    else:
        logger.info("未找到前端构建产物 frontend/dist，请先 `npm run build`（开发模式用 Vite dev server）")

    return app


app = create_app()
