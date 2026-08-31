#!/bin/sh
# ============================================================
# 单元 K · 单元②：容器启动入口（后端与 celery-worker 共用）
#
# 职责：等 PostgreSQL 就绪 → 初始化库（create_all + stamp head）→ 启动主进程。
# 由 docker-compose 设 entrypoint，具体命令作为参数传入（见末尾 exec "$@"）。
#   后端:      ... docker_entrypoint.sh python -m uvicorn app.main:app ...
#   celery:    ... docker_entrypoint.sh celery -A app.core.celery_app worker ...
#
# 注意：数据库建库走 scripts/pg_init.py（create_all + alembic stamp head），
# 不是 alembic upgrade head——历史 SQLite 手写迁移在 PG 上会炸（见 pg_init.py 说明）。
# 脚本由 /bin/sh 执行（避免 Windows 丢失可执行位的问题），无需 chmod +x。
# ============================================================
set -e

echo "[entrypoint] 等待 PostgreSQL 就绪..."
python - <<'PY'
import asyncio
import os
import sys

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

url = os.environ["DATABASE_URL"]

async def wait() -> None:
    for i in range(60):  # 最多约 60 秒
        try:
            eng = create_async_engine(url, pool_pre_ping=True)
            async with eng.connect() as conn:
                await conn.execute(text("SELECT 1"))
            await eng.dispose()
            print("[entrypoint] PostgreSQL 已就绪")
            return
        except Exception as exc:  # noqa: BLE001  连接失败就重试
            if i % 5 == 0:
                print(f"[entrypoint] 等待 PostgreSQL... ({i}s) {type(exc).__name__}")
            await asyncio.sleep(1)
    print("[entrypoint] PostgreSQL 60 秒未就绪，放弃", file=sys.stderr)
    sys.exit(1)

asyncio.run(wait())
PY

echo "[entrypoint] 初始化数据库（create_all + stamp head）..."
python scripts/pg_init.py

echo "[entrypoint] 启动: $*"
exec "$@"
