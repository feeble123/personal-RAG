# 数据备份方案（P2 单元5）

> 真实产品必须有可靠备份。本文档说明当前（PostgreSQL）各数据层的备份与恢复方法。

## 一、数据分层

| 层 | 存储位置 | 备份方式 | 恢复方式 |
|---|---|---|---|
| **关系库（PostgreSQL）** | PG 服务（`rag` 库） | `pg_dump` | `pg_restore` |
| **向量库（Chroma）** | `backend/data/.chroma`（文件） | 复制目录 | 复制回去 |
| **上传原文** | `backend/data/uploads/` | 复制目录 | 复制回去 |
| **隔离区** | `backend/data/uploads/.quarantine/` | 随 uploads 复制（可选） | 复制回去 |
| **配置文件** | `backend/.env` | 单独备份（含密钥，勿外传） | 复制回去 |

> 注意：`backend/data/app.db`（旧 SQLite）已随迁移退役，仅作回退保险暂留。

## 二、PostgreSQL 备份（核心）

### 2.1 逻辑备份（pg_dump，推荐日常用）

```powershell
# 备份到文件（压缩）
pg_dump -U rag_app -h localhost -d rag -Fc -f rag_backup_20260823.dump
# 需要输密码：先设环境变量（Windows PowerShell）
$env:PGPASSWORD = "rag_app_pw_2026"
```

**`-Fc` 是自定义压缩格式**，支持选择性恢复，文件更小。

### 2.2 恢复

```powershell
# 恢复到一个已存在的空库
pg_restore -U rag_app -h localhost -d rag --clean --if-exists rag_backup_20260823.dump
# --clean 会先删旧对象再恢复（覆盖式）
```

> 恢复前建议：目标库先备份一份当前数据，或恢复到新库 `rag_restore` 验证无误后再切。

### 2.3 建议频率

- **开发期**：每次关键改动后手动备份（或用 `start.bat` 里加一键备份）
- **上线后**：定时任务（Windows 计划任务）每天凌晨备份，保留最近 7 份

### 2.4 一键备份脚本（可加进 start.bat 或单独脚本）

```powershell
# backup_pg.ps1 —— 放 backend/scripts/ 下
$env:PGPASSWORD = "rag_app_pw_2026"
$stamp = Get-Date -Format "yyyyMMdd_HHmm"
$dir = "..\data\backups"
New-Item -ItemType Directory -Force -Path $dir | Out-Null
& "C:\Program Files\PostgreSQL\17\bin\pg_dump.exe" -U rag_app -h localhost -d rag -Fc -f "$dir\rag_$stamp.dump"
Write-Output "已备份到 $dir\rag_$stamp.dump"
```

## 三、Chroma 向量库备份

Chroma 是**文件持久化**，备份 = 复制目录：

```powershell
# 备份（先停止应用，避免写入中的不一致）
Copy-Item -Recurse "backend\data\.chroma" "backend\data\backups\.chroma_20260823"

# 恢复
Copy-Item -Recurse "backend\data\backups\.chroma_20260823" "backend\data\.chroma"
```

> **关键**：Chroma 与关系库的 chunk 是配套的（向量按 chunk id 关联）。备份/恢复时两者必须**成对**操作，否则检索会错位。最简单的做法：**整包备份 `backend/data/`**（含 `.chroma`、`uploads`、`.env`）。

## 四、整包备份（最简单，推荐）

```powershell
# 停止应用后，整包备份 data 目录
$stamp = Get-Date -Format "yyyyMMdd_HHmm"
Copy-Item -Recurse "backend\data" "backend\data_backup_$stamp"
```

这会把 PG 之外的所有本地数据（Chroma + uploads + 旧 SQLite）打包。PG 库单独用 `pg_dump`。

## 五、灾难恢复演练（建议每季度一次）

1. 用备份文件恢复到**新库**（`rag_restore`），验证数据完整
2. 停应用，从备份恢复 Chroma + uploads
3. `.env` 临时指向 `rag_restore`，启动应用，抽样问答
4. 验证通过后切回正式库

## 六、密钥与配置备份

- `backend/.env` 含 PG 密码、API 密钥等**敏感信息**——备份时**加密存储**（如 7-Zip 加密压缩），或单独保管
- `.env.example` 是模板（无密钥），可安全进 git
