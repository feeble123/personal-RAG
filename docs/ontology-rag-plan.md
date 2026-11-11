# Ontology RAG 实施计划（已批准）

> 项目：`E:\GPT-Codex\LYTRAG`（分支 `feature/design-v2`）
> 目标：为自动建模 agent 提供可推理、可溯源的本体知识层（REST + MCP）
> 原则：**引擎不认识任何模型**；模型知识只能是 YAML/Excel 数据

---

## 0. 已拍板的决策

| 项 | 决定 |
|---|---|
| 首期交付 | 本体引擎 + Neo4j 存储 + 检索 API |
| TBox 范围 | 调参域 6 类 + 流程骨架占位 4 类 |
| 接入形态 | REST 为主 + MCP 薄封装（stdio） |
| Neo4j | **Neo4j Desktop**（无需 Docker），bolt://localhost:7687 |
| Python | LYTRAG 独立 `.venv`（本机 3.10） |
| 管理接口 | 带最简 CRUD |
| 初始数据 | 按 TDD-XAJ 的 13 个参数（ke/c/wum/wlm/wdm/a_imp/b/sm/ex/ki/kg/ci/cg） |
| 硬约束 | 引擎代码不得出现模型名；不读 `New_Xaj_program` |

---

## 1. 架构

```
TBox（profiles/*.yaml）→ OntologyRegistry → 校验
                                   │
        ABox（Neo4j: OntologyEntity + ONT_REL）
        PG（证据 graph_edge_evidence / 别名 / 运行记录）= 真相源
                                   │
                     检索编排（实体链接 → 1~2 跳 → 事实包）
                              ┌────┴────┐
                    REST /api/v1/ontology/*   MCP（stdio）
```

**不变量**：每条边必须有证据；无证据的数值不进入推荐卡。

| 数据 | 存哪 |
|---|---|
| TBox | YAML（随代码版本化） |
| 实体/关系实例 | Neo4j（可重建的派生索引） |
| 证据 / 别名 / 运行记录 | PostgreSQL |

---

## 2. TBox 规范

```yaml
version: 1
profile_id: demo_tuning      # 引擎只认 profile_id
domain: model_tuning
entity_types:
  - name: Parameter
    display_name: 参数
    uid_prefix: pn           # 2~4 位小写字母，全局唯一
    scope: global            # local=按库隔离 / global=跨库共享
    attributes:
      - { name: group, type: enum, values: [蒸散发, 产流, 水源划分, 汇流, 其他], required: true }
      - { name: physical_low, type: number }
relation_types:
  - { name: AFFECTS, source: Parameter, target: Metric }
constraints:
  - { type: required_attribute, entity: Parameter, attribute: group, severity: error }
```

- 支持 `extends: <profile>` 叠加：具体领域 profile 只写增量。
- 属性类型：`string/text/number/range/enum/list/boolean`。
- 加载即注册到 `graph.schema`，`Node/Edge/GraphStore` 自动识别动态类型。

当前 profile：`app/services/graph/ontology/profiles/demo_tuning.yaml`（10 类实体 / 10 种关系）。

---

## 3. Neo4j 存储

- 节点：`:OntologyEntity {uid, entity_type, name, props_json, kb_ids, doc_ids, version_ids, aliases_text}`
- 关系：`[:ONT_REL {relation_type, version_id, kb_id, doc_id, chunk_ids, evidence_ids, confidence, status, extractor, evidence_text}]`
- 约束与索引：`uid` 唯一；`entity_type`/`kb_id` 索引；`name + aliases_text` 全文索引。
- 写入：`MERGE`（幂等）；列表属性由应用层先读后并集（不依赖 APOC）。
- 版本化：同三元组不同 `version_id` 是不同边；查询只取 active 版本。
- 降级：连接失败/超时 → 记日志与指标，返回空结果，不抛给主链路。

---

## 4. 服务层

| 模块 | 职责 |
|---|---|
| `ontology/registry.py` | YAML 加载/校验/缓存/注册（**O1 已完成**） |
| `ontology/validator.py` | 实例与关系校验（**O1 已完成**） |
| `ontology/instances.py` | 实例 CRUD（O4） |
| `ontology/params.py` | 参数先验导入 + 推荐卡（O3/O4） |
| `ontology/retrieval.py` | 实体链接 → 子图 → 事实包 → 回退（O5） |
| `graph/neo4j_store.py` | Neo4j 实现（O2） |

推荐卡两级：通用范围 → 按条件收窄（`climate=湿润;flood_type=单峰`）→ 冲突并列 → 证据强制。

---

## 5. API 与 MCP

### 对外（`X-API-Key`）
`GET /api/v1/ontology/types|search|entities/{uid}|neighbors/{uid}|params`、`POST /api/v1/ontology/ask`

### 管理（JWT）
`POST /api/admin/ontology/params/import`、`POST/PATCH/DELETE /api/admin/ontology/instances`、`POST /api/admin/ontology/profile/reload`

### MCP（stdio）
`ontology_search`、`ontology_entity`、`ontology_neighbors`、`get_parameter_priors`、`ontology_ask`

**契约**：只新增命名空间，不改 `services/retriever.py` 的冻结字段。

---

## 6. 分阶段

| 单元 | 内容 | 状态 |
|---|---|---|
| O1 | 动态类型兼容层 + Registry/Validator + demo profile | ✅ 已完成 |
| O2 | Neo4jGraphStore（连接/schema/CRUD/查询/降级） | 待做 |
| O3 | 调参+流程 profile、参数表模板与导入 | 待做 |
| O4 | 推荐卡 + 实例 CRUD + 管理 API | 待做 |
| O5 | 检索编排 + 对外 API + 指标 | 待做 |
| O6 | MCP 薄封装 | 待做 |
| O7 | 前端图谱浏览页 | 后续 |
| O8 | 流程本体扩充 + 率定记录回流 | 后续 |

---

## 7. 环境与验证

```powershell
cd backend
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt -i https://mirrors.aliyun.com/pypi/simple/
```

`.env`：`GRAPH_ENABLED=true`、`GRAPH_BACKEND=neo4j`、`GRAPH_NEO4J_URI=bolt://localhost:7687`、`GRAPH_NEO4J_PASSWORD=<Neo4j Desktop 设置的密码>`。

验证：`pytest -q` 全绿；Neo4j 启动后跑通实例写入与邻居查询；停库时主链路不受影响；`grep -ri xaj backend/app/services/graph/ontology/*.py` 零命中。
