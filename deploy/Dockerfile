# ============================================================
# 单元 K · 单元①：多阶段构建（前端构建 → 后端运行时镜像）
#
# 用法（在项目根目录执行，build context = 项目根）：
#   docker build -f deploy/Dockerfile -t rag-system .
# 或走 docker-compose 一键编排（见 deploy/docker-compose.yml）。
#
# 说明：
# - 阶段1 node:20-alpine 构建前端产物（npm ci + vite build）
# - 阶段2 python:3.12-slim 装后端依赖 + 源码 + 前端产物
#   （Python 从 3.10 升到 3.12：3.10 于 2026-10 停维护，容器是全新环境，顺手升级）
# - 运行时数据（uploads / .chroma / PG）由 docker-compose 挂 volume，不进镜像
# ============================================================

# ---- 阶段1：构建前端产物 ----
FROM node:20-alpine AS frontend-builder
WORKDIR /app/frontend

# 先复制依赖清单，利用 Docker 层缓存：package.json/lock 没变就不重装
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci

# 复制前端源码并构建（产出 dist/）
COPY frontend/ ./
RUN npm run build


# ---- 阶段2：后端运行时（Python 3.12）----
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# 系统依赖：onnxruntime / chromadb 的 OpenMP 运行时（RapidOCR 用）
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app/backend

# 依赖清单单独一层，加速缓存；pip 源默认阿里云（服务器在国外可 --build-arg 覆盖）
ARG PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/
COPY backend/requirements.txt ./
RUN pip install -i ${PIP_INDEX_URL} -r requirements.txt

# 后端源码 + 迁移 + 脚本
COPY backend/app ./app
COPY backend/alembic ./alembic
COPY backend/alembic.ini ./
COPY backend/scripts ./scripts

# 前端构建产物（app/main.py 按 parents[2]/frontend/dist 定位 → /app/frontend/dist）
COPY --from=frontend-builder /app/frontend/dist /app/frontend/dist

EXPOSE 8000

# 生产部署：uvicorn 单进程；横向扩展（多 worker / Celery）见 docker-compose 编排
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
