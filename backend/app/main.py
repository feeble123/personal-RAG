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
from app.core.logging import setup_logging
from app.core.ratelimit import limiter
from app.db.session import async_session_factory, ensure_db_at_head, init_db

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
                    role="superadmin",
                )
            )
            await db.commit()
            logger.info("已创建管理员账号: %s", settings.admin_username)


async def _warmup_bm25() -> None:
    """启动预热：从 DB 重建各知识库 BM25 语料，避免重启后首问慢/无 BM25 结果。"""
    try:
        from sqlalchemy import select

        from app.db.models import Chunk, Document
        from app.services import bm25
        # jieba 默认 DEBUG 级并自带无格式 stderr handler，启动时刷屏「分词词典加载」日志；
        # 提到 WARNING 让启动日志干净。注意必须在 import 之后设置（jieba import 时强制回 DEBUG）。
        logging.getLogger("jieba").setLevel(logging.WARNING)

        async with async_session_factory() as db:
            # P1-2 修复：只保留 active 版本的切片。文档重灌后旧版本标记 retired，
            # 但 chunk 行仍留在 DB（回滚保险）；BM25 若把它们也灌进内存，检索会把
            # 旧版本的垃圾 section / 空公式顶进数据源（用户实测公式(9.95)命中 retired 旧块）。
            rows = await db.execute(
                select(Chunk.kb_id, Chunk.id, Chunk.content)
                .join(Document, Chunk.doc_id == Document.id)
                .where(Chunk.document_version_id == Document.active_version_id)
                .order_by(Chunk.id)
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
    # P2 单元2：统一日志配置在 lifespan 执行（而非模块 import 时）——
    # 避免 pytest 收集阶段（import app.main）触发与 pytest logging 插件的冲突
    setup_logging()
    # 准备数据目录（quarantine 隔离区随 uploads 一并创建）
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.upload_dir_path.mkdir(parents=True, exist_ok=True)
    settings.quarantine_dir_path.mkdir(parents=True, exist_ok=True)
    settings.chroma_dir_path.mkdir(parents=True, exist_ok=True)
    # 单元 D：启动时清理 Chroma 残留孤儿 HNSW 目录（delete 后句柄未释放，只能在启动时安全 move）
    from app.services import vector_store

    vector_store.gc_orphan_hnsw_dirs()
    # 建表 + 种子 + 预热 + 清空语义缓存（防旧检索答案残留劫持新检索）
    await init_db()
    # P0-6：检查迁移版本是否 head（只检查不自动迁移）；P2 单元1 异步化（PG 兼容）
    await ensure_db_at_head()
    await _seed_admin()
    await _warmup_bm25()
    from app.services import semantic_cache

    await semantic_cache.clear_cache()
    # P0-9：启动后台入库 worker（轮询 DB job 表；进程内常驻）
    from app.modules.ingestion import manager as ingestion_manager

    ingestion_manager.start_worker()
    logger.info("应用启动完成: %s", settings.app_name)
    yield
    # 优雅关闭：停掉 worker（置停止标记 + 取消轮询），避免任务残留
    ingestion_manager.stop_worker()
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

    # P2 单元2：请求日志（最外层，罩住 429/OPTIONS；不消费 body，SSE 安全）
    from app.core.middleware import RequestLoggingMiddleware

    app.add_middleware(RequestLoggingMiddleware)

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

    # 健康检查（P2 单元2 真探活）：摸数据库脉搏；可选探 Chroma（默认关保持快）
    @app.get("/api/health")
    async def health() -> JSONResponse:
        import sqlalchemy as sa

        checks: dict = {}
        ok = True
        try:
            async with async_session_factory() as db:
                await db.execute(sa.text("SELECT 1"))
            checks["db"] = "ok"
        except Exception as exc:  # noqa: BLE001  健康检查需覆盖任何 DB 故障
            ok = False
            checks["db"] = f"error: {exc!r}"
        if settings.health_check_chroma:
            try:
                import asyncio

                from app.services import vector_store

                await asyncio.to_thread(vector_store.count)
                checks["chroma"] = "ok"
            except Exception as exc:  # noqa: BLE001
                ok = False
                checks["chroma"] = f"error: {exc!r}"
        return JSONResponse(
            status_code=200 if ok else 503,
            content={
                "status": "ok" if ok else "degraded",
                "app": settings.app_name,
                "version": "1.0.0",
                "checks": checks,
            },
        )

    # Prometheus 指标（P2 单元3）：必须在 SPA fallback 之前注册，否则被 catch-all 吞掉
    if settings.metrics_enabled:
        import sqlalchemy as sa

        from fastapi.responses import Response as FastAPIResponse

        from app.core.metrics import generate_metrics_text, refresh_active_jobs, refresh_queue_metrics
        from app.db.models import IngestionJob

        @app.get("/metrics")
        async def metrics_endpoint() -> FastAPIResponse:
            # 刷新活跃入库任务数 + 各阶段积压指标（DB 挂则沿用旧值，不阻塞）
            try:
                async with async_session_factory() as db:
                    cnt = (
                        await db.execute(
                            sa.select(sa.func.count())
                            .select_from(IngestionJob)
                            .where(IngestionJob.stage.in_(("queued", "parsing", "embedding", "indexing")))
                        )
                    ).scalar_one()
                    refresh_active_jobs(int(cnt))
                # 单元 J 单元⑤：各阶段在途数 + 最老等待时长（分层定位「哪一环堵了」）
                from app.modules.ingestion.manager import queue_backlog_snapshot

                refresh_queue_metrics(await queue_backlog_snapshot())
            except Exception:  # noqa: BLE001
                pass
            return FastAPIResponse(generate_metrics_text(), media_type="text/plain")

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
