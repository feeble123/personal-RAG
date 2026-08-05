# 升级路径：Docker 单镜像部署（后端 + 前端构建产物）
# 使用：docker build -t rag-system . && docker run -p 8000:8000 -v rag-data:/app/backend/data rag-system
FROM python:3.10-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# 后端依赖
COPY backend/requirements.txt backend/
RUN pip install -r backend/requirements.txt

# 后端源码
COPY backend/app backend/app
COPY backend/scripts backend/scripts

# 前端构建产物（若需在镜像内构建请使用多阶段构建）
COPY frontend/dist frontend/dist

WORKDIR /app/backend
EXPOSE 8000

CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
