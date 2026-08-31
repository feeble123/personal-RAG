# 单元 K：Docker / CI-CD（只写不跑，为上线铺路）

> 制定日期：2026-08-30
> 背景：P2-4 原计划项。用户已拍板 **「只写不跑」**——本机开不了虚拟化、装不了 Docker，
> 因此本单元把 Docker/CI 全套配置**写好、写正确**，但**不在本机 build 验证**。
> 等将来有 Linux 服务器（或本机开虚拟化装 Docker）后，`docker compose up` 即可真跑。
> 执行节奏：**分单元，每单元完成停等验收**。

---

## 一、为什么做（大白话）

现在系统跑在「你这一台 Windows 电脑」上，靠一堆手动配的环境（PG、Redis、Chroma、Python、Node）。
换台机器、或将来上线租台服务器，要重配一遍，费劲还容易漏。

Docker = 把整套系统装进「标准箱子」：
- **一条命令**把箱子搬去任何 Linux 服务器直接跑；
- 顺便把单元 J 做出来的 **Redis + Celery worker 也编排进去**，兑现「多机横向扩展」的价值；
- **CI/CD** = 以后每次改代码 push 到 GitHub，云端自动帮你跑一遍测试，坏了立刻知道。

## 二、诚实的前提（必读）

1. **本机不能验证**：你电脑 BIOS 关了虚拟化，装不了 Docker。本单元所有文件「写得对、跑不了」，
   只能靠代码审查 + 与既有 `deploy/` 配置逐项核对来保证正确性。真跑要等有 Linux 环境。
2. **CI 要 push 才生效**：GitHub Actions 的测试跑在云端，本机不触发。我们遵守「不 push 远程」的规矩，
   所以 workflow 文件**写好但暂时不触发**，等将来你决定 push 到远程时自然生效。
3. **这不是性能刚需**：单机其实够用。Docker 的价值在「部署省心 + 多机扩展」，是为将来铺路。

## 三、现状（已核实）

- `deploy/` 目录是 **SQLite 时代的老配置**：Dockerfile 用 python:3.10-slim、compose 只有 rag 一个服务、
  没 Redis、没 Celery、没 PG——全都要升级到当前 PG17 + Redis + Celery 形态。
- 前端构建：`npm run build`（tsc -b && vite build），用 npm（有 package-lock.json）。
- PG 建库正确路径：`scripts/pg_init.py`（create_all + stamp head），**不是** alembic upgrade head（历史 SQLite 迁移在 PG 会炸）。
- 无 `.dockerignore`（现有 .gitignore 已有 .venv/node_modules/dist 规则）。

## 四、分 4 个单元

### 单元①：Dockerfile + .dockerignore

- 多阶段构建：**阶段1** node:20 构建前端 → **阶段2** python:3.12-slim 装后端依赖 + 复制源码 + 前端产物
- 顺便把 Python 从 3.10 → **3.12**（3.10 今年 10 月停维护，上线前该升，容器里一并解决）
- `.dockerignore` 排除 .venv / node_modules / data / dist / 测试缓存等大目录，镜像瘦身

**验收**：`docker build -t rag-system .` 语法正确（本机无 docker，靠审查 + Dockerfile 规范核对）

### 单元②：docker-compose + 容器启动脚本

- 服务编排：`db`(PG17) + `redis` + `backend`(uvicorn) + `celery-worker` + `nginx`
- 数据卷：pgdata / uploads / chroma（真实数据不丢）
- 启动脚本（entrypoint）：等 PG 就绪 → `pg_init.py` 建库 → 起 uvicorn；celery worker 单独入口
- 健康检查：backend 探 `/api/health`、db 探 `pg_isready`、redis 探 `ping`

**验收**：compose 服务拓扑完整、env 映射正确、启动顺序（depends_on + healthcheck）合理

### 单元③：CI/CD（GitHub Actions）

- workflow：push/PR 触发 → 装后端依赖 → pytest → 前端 lint + test → （可选）docker build
- 用阿里云镜像装 pip 依赖（与本机一致）
- **标注：需 push 到远程才触发，本机不跑**

**验收**：workflow 语法正确、步骤齐全、覆盖后端测试 + 前端检查

### 单元④：部署文档更新

- 更新 `docs/P2-DEPLOYMENT.md`：从「SQLite 老配置」更新为「PG + Redis + Celery 一键部署」真实步骤
- 写清：Linux 服务器怎么起、数据怎么挂载备份、生产 fail-safe 怎么开

**验收**：文档与实际 compose 配置一致，能照着一步步跑

---

## 五、风险与诚实提醒

1. **「只写不跑」的最大风险是「写得对但没人验证过」**——将来第一次真 build 可能暴露小问题（如依赖版本、路径）。这是可接受的：真跑时修，不阻塞现在。
2. **Python 3.12 升级**：单元① 在容器里把 Python 升到 3.12。本机仍是 3.10（不动），容器与本地暂时不同步——这没问题，因为容器是独立环境。
3. **不碰真实数据**：所有 volume 挂载都是「引用」你现有 data 目录，本单元不删、不改任何真实数据。

## 六、已拍板（2026-08-30）

1. **执行方式**：**只写不跑**（本机开不了虚拟化，无法 build 验证）。
2. **CI 触发**：workflow 写好但不 push，等将来用户决定 push 远程时自然生效。
