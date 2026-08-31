# 部署形态指南（P2 单元5 · 单元 K 更新）

> 真实产品从单机开发到生产部署的演进路径。本文基于当前 `deploy/` 目录（Dockerfile / docker-compose / nginx.conf）与「PG + Redis + Celery」架构（单元 J/K），说明当前形态与各演进阶段的做法。
>
> **状态说明**：Docker/CI 全套配置已写好但**未在本机 build 验证**（本机关了虚拟化、装不了 Docker）。真跑要等有 Linux 服务器（或本机开虚拟化装 Docker），见文末「七、诚实的前提」。

## 一、当前形态（单机开发）

```
本机 Windows：
  uvicorn 单进程 (FastAPI) ──▶ PostgreSQL 17 (本地服务)
                        ──▶ Chroma (.chroma 文件，嵌入式)
                        ──▶ 进程内 asyncio worker（领 DB job 干活）
                        ──▶ 前端构建产物 (frontend/dist，由 FastAPI 托管)
```

- 启动：`start.bat`（检查环境 → `npm run build` → uvicorn）
- 数据库：本地 PostgreSQL 服务（`rag_app` 账号 / `rag` 库）
- 向量库：嵌入式 Chroma（文件持久化，随 data 目录）
- 后台任务：进程内 worker（`USE_CELERY=false` 默认），DB 的 IngestionJob 表是任务真相源
- 适合：开发、演示、小规模内部使用

## 二、演进阶段总览

| 阶段 | 形态 | 适用 | 关键变化 |
|---|---|---|---|
| **1. 单机**（当前） | 本机 uvicorn + PG + Chroma + 进程内 worker | 开发/内部 | — |
| **2. Docker 容器化** | 容器跑 PG + Redis + 后端 + Celery worker + nginx | 单台服务器部署 | `docker compose up` 一键起五服务 |
| **3. 多服务上云** | 后端 + PG + 对象存储 + 反向代理 | 对外服务 | 拆分服务、上云 RDS、加 TLS |
| **4. 横向扩展** | 多实例 + 集中向量库 | 高并发 | pgvector/Milvus + 负载均衡 + 多 worker |

## 三、阶段 2：Docker 容器化（单台服务器，已写好）

### 3.1 服务拓扑

`deploy/docker-compose.yml` 已编排好 **5 个服务**：

```text
客户端 ──> nginx(80) ──> backend(uvicorn:8000) ──读写──> db(PG17)
                              │                          │
          celery-worker ──共享卷──> uploads/Chroma        └── redis(broker)
                              └──队列调度──> redis
```

| 服务 | 镜像 | 职责 |
| --- | --- | --- |
| db | postgres:17-alpine | 关系库（真相源），数据挂 `pgdata` 卷 |
| redis | redis:7-alpine | 仅 broker 调度器（容器内是新版 Redis 7，非本机 3.0） |
| backend | 本项目 Dockerfile | FastAPI；`USE_CELERY=true` 时本进程只做 reaper 回收，不抢活 |
| celery-worker | 本项目 Dockerfile | 领 DB job 干活（解析/打向量/写库），与 backend 共享 uploads/Chroma 卷 |
| nginx | nginx:1.27-alpine | 对外唯一入口，SSE 关缓冲 |

### 3.2 启动（一键）

```bash
# 在项目根目录，先备好 backend/.env（生产密钥 + 强密码，见 4.3）
docker compose -f deploy/docker-compose.yml up -d --build
```

启动顺序由 `depends_on` + `healthcheck` 保证：db 就绪 → redis 就绪 → backend / celery-worker。两者启动时都会执行 `scripts/docker_entrypoint.sh`：

1. 等 PostgreSQL 可连（最多 60 秒）
2. `python scripts/pg_init.py` 初始化库（create_all + alembic stamp head）
3. 启动主进程（backend 跑 uvicorn；worker 跑 celery）

> **建库走 `pg_init.py`，不是 `alembic upgrade head`**——历史 SQLite 手写迁移在 PG 上会炸（见 `scripts/pg_init.py` 说明）。脚本幂等，两个服务重复跑不报错。

### 3.3 数据卷（真实数据不丢）

| 卷 | 挂载点 | 内容 |
| --- | --- | --- |
| pgdata | /var/lib/postgresql/data | PG 数据 |
| uploads | /app/data/uploads | 上传原文 + 隔离区 |
| chroma | /app/data/.chroma | 向量库 |

## 四、阶段 3：多服务上云（对外服务）

### 4.1 架构

```text
用户浏览器 ──▶ Nginx (443 TLS) ──▶ FastAPI (uvicorn)
                        ├──▶ PostgreSQL (云 RDS，自动备份)
                        ├──▶ Redis (云 Redis，Celery broker)
                        ├──▶ Celery worker（可独立扩几台）
                        └──▶ Chroma（容器卷，或迁 pgvector/Milvus）
```

### 4.2 用现有 nginx.conf 反代

`deploy/nginx.conf` 已写好：全部路径反代到 `backend:8000`（含 SSE 关缓冲、`/assets/` 长缓存）。云上部署时：

1. 申请域名 + TLS 证书（Let's Encrypt）
2. nginx 监听 443，把流量交给 backend
3. PostgreSQL / Redis 用云托管（自带快照备份，省去自管）

### 4.3 环境变量规范（生产，必读）

容器内 `APP_ENV=production`，启动 fail-safe 会强制要求：

- `JWT_SECRET` 非空且是强随机串
- `ADMIN_PASSWORD` 非空且非 `123456`
- `EMBEDDING_API_KEY` / `DEEPSEEK_API_KEY` 非空
- `DEBUG=false`

**缺任一直接拒绝启动**（P0-1 设计保护）。密钥从 `backend/.env` 经 `env_file` 注入，**不进镜像**（`.dockerignore` 已排除 `.env`）。

### 4.4 Redis/Celery 的诚实边界

- **当前 worker 只开 1 个**：manager 的「查重 + 写入」锁是**每进程**的，多 worker 会失去跨进程串行保护。多 worker 横向扩展前，需先给内容去重加数据库级唯一约束。
- **Windows 上的 Celery 是打折扣的**：官方对 Windows 多进程 pool 支持有限（本机用 `--pool=solo`）。真正的多机扩展靠 Linux（本 compose 即 Linux），届时才完全兑现。

## 五、阶段 4：横向扩展（高并发，答辩后规划）

- **关系库**：PG 已就绪，可上云 RDS 主从
- **向量库**：当前 Chroma 嵌入式是单机文件——高并发需迁 **pgvector**（PG 扩展）或 Milvus/Qdrant
- **应用**：uvicorn 多 worker / gunicorn；无状态化（会话/缓存外置）
- **Celery**：多 worker + 队列分层（6 队列已定义：ingestion.control / parser.cpu / parser.gpu / embedding / indexing / maintenance）
- **监控**：Prometheus 拉 `/metrics`（含队列积压指标）+ Grafana 看板
- **日志**：`LOG_JSON=true` 结构化日志，对接 Loki/ELK

## 六、当前可直接做的生产加固（低成本）

1. **日志**：`.env` 设 `LOG_JSON=true` + 采集到 stdout（对接 Loki）
2. **监控**：起一个 Prometheus 拉 `/metrics`（已埋点：问答/检索/入库/队列积压），先出曲线
3. **备份**：按 `docs/P2-BACKUP.md` 建定时备份（pg_dump + Chroma + 整包）
4. **健康检查**：负载均衡器用 `/api/health`（真探活，DB 挂返回 503）

## 七、诚实的前提（必读）

1. **Docker 配置「只写不跑」**：本机 BIOS 关虚拟化、装不了 Docker。上面所有文件「写得对、但没真 build 过」，靠代码审查 + 逐项核对保证正确性。**将来第一次真跑可能暴露小问题**（依赖版本、路径），真跑时修即可，不阻塞现在。
2. **CI 要 push 才生效**：`.github/workflows/ci.yml` 已写好（后端 pytest + 前端 lint/test/build），但需 push 到 GitHub 才触发。遵守「不 push 远程」规矩，暂不触发。
3. **Python 3.12 只在容器里升**：容器用 `python:3.12-slim`（3.10 于 2026-10 停维护），本机仍是 3.10，二者独立、互不影响。

## 八、安全清单（上线前必查）

- [ ] `APP_ENV=production`，强 JWT_SECRET / 强 admin 密码
- [ ] PostgreSQL 密码非默认（compose 里 `POSTGRES_PASSWORD` 用环境变量覆盖，勿用默认 `rag_app_password`）
- [ ] `.env` 不进 git、不进镜像（`.dockerignore` 已排除）
- [ ] 上传隔离（P0-10）、MIME 校验生效
- [ ] nginx 只暴露 80/443，内部端口（8000/5432/6379）不对外映射
- [ ] 备份脚本就绪 + 恢复演练过
