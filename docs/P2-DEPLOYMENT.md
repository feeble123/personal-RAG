# 部署形态指南（P2 单元5）

> 真实产品从单机开发到生产部署的演进路径。本文基于现有 `deploy/` 目录（Dockerfile / docker-compose / nginx.conf），说明当前形态与各演进阶段的做法。

## 一、当前形态（单机开发）

```
本机 Windows：
  uvicorn 单进程 (FastAPI) ──▶ PostgreSQL 17 (本地服务)
                        ──▶ Chroma (.chroma 文件)
                        ──▶ 前端构建产物 (frontend/dist，由 FastAPI 托管)
```

- 启动：`start.bat`（检查环境 → `npm run build` → uvicorn）
- 数据库：本地 PostgreSQL 服务（`rag_app` 账号 / `rag` 库）
- 向量库：嵌入式 Chroma（文件持久化，随 data 目录）
- 适合：开发、演示、小规模内部使用

## 二、演进阶段总览

| 阶段 | 形态 | 适用 | 关键变化 |
|---|---|---|---|
| **1. 单机**（当前） | 本机 uvicorn + PG + Chroma | 开发/内部 | — |
| **2. Docker 容器化** | 容器跑后端 + PG | 单台服务器部署 | 用 docker-compose 编排 |
| **3. 多服务上云** | 后端 + PG + 对象存储 + 反向代理 | 对外服务 | 拆分服务、加 nginx |
| **4. 横向扩展** | 多实例 + 集中向量库 | 高并发 | pgvector/Milvus + 负载均衡 |

## 三、阶段 2：Docker 容器化（单台服务器）

### 3.1 改造现有 docker-compose（补 PG 服务）

现有 `deploy/docker-compose.yml` 只有 `rag` 服务（SQLite 时代）。升级为含 PG：

```yaml
version: "3.8"
services:
  rag:
    build: ..
    container_name: rag-system
    ports:
      - "8000:8000"
    env_file:
      - ../backend/.env
    volumes:
      - rag-uploads:/app/backend/data/uploads
      - rag-chroma:/app/backend/data/.chroma
    depends_on:
      - db
    restart: unless-stopped

  db:
    image: postgres:17
    container_name: rag-db
    environment:
      POSTGRES_USER: rag_app
      POSTGRES_PASSWORD: rag_app_pw_2026
      POSTGRES_DB: rag
    volumes:
      - pgdata:/var/lib/postgresql/data
    ports:
      - "5432:5432"
    restart: unless-stopped

volumes:
  rag-uploads:
  rag-chroma:
  pgdata:
```

### 3.2 启动

```bash
cd deploy
docker compose up -d --build
# 首次需初始化 PG 库（进容器或本机执行）：
docker exec rag-db psql -U rag_app -d rag -c "CREATE EXTENSION IF NOT EXISTS vector;"
```

> 若本机 PG 已占用 5432，compose 的 `db` 服务改映射 `5433:5432`，`.env` 的 DATABASE_URL 相应改端口。

## 四、阶段 3：多服务上云（对外服务）

### 4.1 架构

```
用户浏览器 ──▶ Nginx (443 TLS) ──▶ FastAPI (uvicorn)
                        ├──▶ PostgreSQL (云 RDS，自动备份)
                        ├──▶ Chroma (可保留容器卷，或迁 pgvector)
                        └──▶ 前端构建产物 (Nginx 托管 /api 反代)
```

### 4.2 用现有 nginx.conf 反代

`deploy/nginx.conf` 已写好：`/api/` 反代到后端（SSE 关缓冲）、`/assets/` 长缓存、SPA fallback。云上部署时：
1. 申请域名 + TLS 证书
2. nginx 监听 443，把 `/api/` 与前端静态资源都交给 nginx
3. PostgreSQL 用云 RDS（自带快照备份，省去自管）

### 4.3 环境变量规范（生产）

生产必须设 `APP_ENV=production`——启动 fail-safe 会强制要求强 JWT_SECRET / 强 admin 密码 / API 密钥，缺任一直接拒绝启动（P0-1）。**严禁 production 用默认密钥上线。**

## 五、阶段 4：横向扩展（高并发，答辩后规划）

- **关系库**：PG 已就绪，可上云 RDS 主从
- **向量库**：当前 Chroma 嵌入式是单机文件——高并发需迁 **pgvector**（PG 扩展）或 Milvus/Qdrant（P1-8 已规划，答辩后）
- **应用**：uvicorn 多 worker 或 gunicorn；无状态化（会话/缓存外置）
- **监控**：Prometheus 拉 `/metrics` + Grafana 看板（P2 单元3 已埋点）
- **日志**：`LOG_JSON=true` 输出结构化日志，对接 Loki/ELK

## 六、当前可直接做的生产加固（低成本）

1. **日志**：`.env` 设 `LOG_JSON=true` + 日志采集到文件/stdout（P2 单元2 已支持）
2. **监控**：起一个 Prometheus 拉 `/metrics`（P2 单元3 已埋点），先出曲线
3. **备份**：按 `docs/P2-BACKUP.md` 建定时备份
4. **健康检查**：负载均衡器用 `/api/health`（真探活，DB 挂返回 503）

## 七、安全清单（上线前必查）

- [ ] `APP_ENV=production`，强 JWT_SECRET / 强 admin 密码
- [ ] PostgreSQL 密码非默认，`pg_hba.conf` 只允许必要来源
- [ ] `.env` 不进 git、不外传
- [ ] 上传隔离（P0-10）、MIME 校验生效
- [ ] nginx 只暴露 443/80，内部端口（5432）不对外开放
- [ ] 备份脚本就绪 + 恢复演练过
