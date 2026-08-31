---
name: unit-test
description: 运行本项目全量测试（后端 pytest：单元 + 集成），输出测试报告
---

# 单元测试

运行本项目的全面测试 → 输出测试报告。

> 本技能从「Study vibe coding」项目迁移，已适配当前项目（LangChain RAG 知识库问答系统）。
> 当前项目技术栈：Python 后端（FastAPI）+ React 前端，测试框架为 **pytest**。

## 步骤

1. **运行全量测试**（单元 + 集成，一条命令搞定）：
   ```bash
   cd "e:/GPT-Codex/LangChainRAG/backend" && PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest -q
   ```
   - 全部测试：单元测试在 `tests/unit/`（服务层纯逻辑：security / chunker / parser / rag / bm25 / semantic_cache / embedding），集成测试在 `tests/` 根目录（`test_api.py`，端到端 API 流程）。
   - 只跑单元测试：`-q tests/unit`
   - 只跑某个模块：`-q tests/unit/test_chunker.py`

2. **输出测试报告**：给用户一张成绩单：
   - 一共几个测试、通过几个、失败几个、耗时
   - 失败的每条：测试名 + 期望结果 vs 实际结果 + 原因
   - 若是代码 bug，说明并修复后重跑，直到全绿

## 编写/更新测试

- **单元测试**（逻辑层，不依赖 DB/网络）：放 `backend/tests/unit/`，命名 `test_*.py`。
  - 纯函数直接测；解析器用 `tests/data/` 现成样本（`shuili.md` / `shuili.pdf` / `scan_sample.pdf`）。
  - embedding 用 `FakeEmbedding`（确定性哈希，无需 API Key）。
- **集成测试**（走完整 API + 数据库）：放 `backend/tests/` 根目录，复用 `conftest.py` 的 `client` / `admin_headers` / `user_headers` / `sample_kb` fixture（自动隔离临时数据目录 + FAKE 离线模型）。

## 备注

- ⚠️ Windows 控制台中文输出必须带 `PYTHONIOENCODING=utf-8`，否则 GBK 编码会乱码/报错。
- 被测代码改动后，先跑全量确认未破坏，再给新功能补新测试。
- 前端（可选）若需测 TS 逻辑：`cd frontend && npm install -D vitest`，测试放 `src/**/*.test.ts`，用 `node node_modules/vitest/vitest.mjs run`（路径含 `&`，勿用 `npm test`）。
