# PostgreSQL 升级路径（P2 单元1：应用层就绪）

> 本文档说明如何把本项目从 **SQLite 切换到 PostgreSQL**。P2 单元1 已完成应用层接线
> （代码/脚本/守卫/文档），本机当前仍跑 SQLite（222MB 真实数据不动）；真正迁移按本文档执行。

## 一、背景与原则

**为什么 PG 建库不走历史迁移链**：`backend/alembic/versions/` 里有 3 个手写 **SQLite 专用 DDL** 迁移：

| 迁移 | 内容 | 在 PG 上的问题 |
|---|---|---|
| `a1b2c3d4e5f6` | chunks 表重建（唯一约束） | `INTEGER NOT NULL PRIMARY KEY`（PG 不自增）、`ALTER TABLE ... RENAME` 不可用 |
| `b2c3d4e5f6a7` | citations 表重建 | `DATETIME`（PG 无此类型，语法级失败） |
| `c3d4e5f6a7b8` | chunks 表重建（挂版本） | 同上 |

这 3 个迁移在 SQLite 上重放没问题（已加 `assert_sqlite_or_raise` 守卫，SQLite 放行、PG 响亮报错），
但在 PG 上 `upgrade head` 会直接失败。**PG 是正确的全新空库，不需要也不应该跑历史迁移链**。

**核心原则**：`Base.metadata.create_all()`（方言原生 DDL，一次建出与当前 ORM 完全一致的完整 schema）+
`alembic stamp head`（标记已到最新，未来新迁移照常 `upgrade head`）。

## 二、切换 PG 标准操作（一步到位）

### 1. 安装 PostgreSQL（Windows，需管理员）

```powershell
winget install PostgreSQL.PostgreSQL.17 --accept-package-agreements --accept-source-agreements
```

安装器会要求设 **postgres 超级用户密码**（请记牢）。装完注册为 Windows 服务、开机自启、提供 pgAdmin。

### 2. 建库 + 建应用用户

用 psql（或 pgAdmin 图形界面）：

```sql
CREATE USER rag_app WITH PASSWORD 'rag_app_password';
CREATE DATABASE rag OWNER rag_app;
```

> 生产环境请改用强密码，不要用示例 `rag_app_password`。

### 3. 安装依赖（已含 asyncpg）

```powershell
cd backend
.venv/Scripts/pip install -r requirements.txt -i https://mirrors.aliyun.com/pypi/simple/
```

### 4. 配置 .env

`backend/.env` 里设：

```
DATABASE_URL=postgresql+asyncpg://rag_app:rag_app_password@localhost:5432/rag
```

### 5. 初始化 PG 库（create_all + stamp head）

```powershell
.venv/Scripts/python.exe scripts/pg_init.py
```

### 6. 校验

```powershell
.venv/Scripts/python.exe -m alembic current        # 应显示 head（f9a0b1c2d3e4 或更新）
```

启动应用后：`curl http://localhost:8000/api/health` → `checks.db=ok`。

## 三、数据迁移（可选，二选一）

### 方案 A：重新上传入库（推荐，最干净）

PG 作为全新库，把 `backend/data/uploads/` 里的 14 个原始文档通过前端重新上传入库。
Chroma 向量一并重建，无历史数据不一致风险。适合：demo 数据 / 评测集需重灌。

### 方案 B：脚本迁移历史数据（保留历史消息/引用）

见 P2 单元4 `backend/scripts/migrate_sqlite_to_pg.py`（逐表迁移 + 自增序列同步 + count 校验）。
迁移前先 **备份 SQLite**（`data/app.db` 拷一份）；迁移后 `.env` 改回 sqlite 连接串重启即可回退，
两份数据并存互不影响。

## 四、未来 schema 变更

PG 环境下 schema 变更照常走 Alembic：

```powershell
.venv/Scripts/python.exe -m alembic revision --autogenerate -m "描述"
.venv/Scripts/python.exe -m alembic upgrade head
```

autogenerate 对 PG 生成原生 `ALTER TABLE`（无需 SQLite 的 batch 换表），更干净。

## 五、回退

`.env` 的 `DATABASE_URL` 改回 SQLite 连接串，重启即可。SQLite 数据文件仍在（`data/app.db`），
PG 数据也保留，互不影响。适合：迁移验证失败 / 想回滚到原环境。

## 六、测试

- 离线测试（默认）：`pytest` 全绿，跑在临时 SQLite 上（conftest 隔离），与 PG 无关。
- PG 集成测试（可选）：设 `RUN_PG_TESTS=1` 后 `pytest -m pg`——需先完成 PG 初始化。
- 迁移文件守卫：`tests/unit/test_pg_readiness.py` 离线覆盖 `assert_sqlite_or_raise` 逻辑。

## 七、已知限制

- `.contains(q)`（用户/记忆搜索）在 PG 下 `LIKE` 大小写敏感（SQLite 大小写不敏感）——如需要可改 `ilike`。
- `JSON` 列在 PG 是 `json`（非 `jsonb`），本项目只用存取不用查询，行为等价。
- `DateTime` 无 `timezone=True`（PG 是 `TIMESTAMP` 非 `TIMESTAMPTZ`），时间戳语义与 SQLite 一致（均视为本地/无时区）。
- **Chroma 仍是本地向量库**（`data/.chroma` 文件持久化，独立于关系库），切 PG 后向量检索不受影响；
  切 pgvector/Milvus/Qdrant 属另一个项目（P1-8，答辩后规划）。
