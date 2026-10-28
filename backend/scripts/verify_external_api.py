"""单元 1 端到端验收脚本：真实启动服务 → 入库 → 调对外检索 API。

特点：
- 使用**临时 SQLite + 临时 Chroma + 临时 uploads**，完全不碰主库/真实数据
- 使用 fake embedding/LLM，离线可跑（无需 API Key）
- 真实启动 uvicorn 子进程，用 HTTP 调用，和生产调用方式一致
- 覆盖：服务健康 → 登录 → 建库 → 上传 → 入库 → 外部 API 鉴权 → 真实检索 → 契约校验

运行（在 backend 目录，使用项目 venv）：
    .venv/Scripts/python.exe -X utf8 scripts/verify_external_api.py
"""
from __future__ import annotations

import asyncio
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

BASE_DIR = Path(__file__).resolve().parents[1]  # backend/
PORT = 8011
BASE = f"http://127.0.0.1:{PORT}"
API_KEY = "verify-external-key-123"

MD = """# 水利工程基础

## 明渠均匀流

明渠均匀流的形成条件包括：长直棱柱体渠道、正坡、糙率不变、流量恒定。

## 防汛要求

防汛工作应坚持安全第一、常备不懈、以防为主、全力抢险的方针。
"""


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", port)) != 0


def _ok(msg: str) -> None:
    print(f"  [OK] {msg}")


def _fail(msg: str) -> None:
    raise AssertionError(msg)


async def _wait_ready(
    client: httpx.AsyncClient,
    proc: subprocess.Popen,
    log_path: Path,
    timeout: float = 60.0,
) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            tail = log_path.read_text(encoding="utf-8", errors="replace")[-4000:]
            _fail(f"服务进程已退出（code={proc.returncode}）\n--- 日志尾部 ---\n{tail}")
        try:
            r = await client.get(f"{BASE}/api/health", timeout=2)
            if r.status_code == 200:
                return
        except Exception:
            pass
        await asyncio.sleep(0.5)
    tail = log_path.read_text(encoding="utf-8", errors="replace")[-4000:]
    _fail(f"服务 60 秒内未就绪\n--- 日志尾部 ---\n{tail}")


async def _wait_ingest(client: httpx.AsyncClient, headers: dict, kb_id: int, timeout: float = 90.0) -> None:
    deadline = time.time() + timeout
    last = "unknown"
    while time.time() < deadline:
        r = await client.get(f"{BASE}/api/admin/kbs/{kb_id}/documents", headers=headers)
        r.raise_for_status()
        items = r.json()["items"]
        if items:
            last = items[0]["status"]
            if last == "ready":
                return
            if last == "failed":
                _fail(f"入库失败: {items[0].get('error_message')}")
        await asyncio.sleep(0.5)
    _fail(f"入库 90 秒未完成（最后状态: {last}）")


async def run() -> None:
    if not _port_free(PORT):
        _fail(f"端口 {PORT} 已被占用，请先关闭占用进程")

    tmp = tempfile.mkdtemp(prefix="verify_ext_api_")
    env = os.environ.copy()
    env.update(
        {
            "APP_ENV": "test",
            "DATABASE_URL": f"sqlite+aiosqlite:///{tmp}/test.db",
            "CHROMA_DIR": f"{tmp}/chroma",
            "UPLOAD_DIR": f"{tmp}/uploads",
            "EMBEDDING_PROVIDER": "fake",
            "LLM_PROVIDER": "fake",
            "RERANK_ENABLED": "false",
            "EXTERNAL_API_KEY": API_KEY,
            "JWT_SECRET": "verify-secret-not-production",
            "ADMIN_PASSWORD": "123456",
            "PYTHONIOENCODING": "utf-8",
        }
    )

    print("=" * 62)
    print("单元 1 端到端验收：对外检索 + 问答 API")
    print("=" * 62)
    print(f"临时数据目录: {tmp}")

    # G5-3：应用启动只检查迁移版本、不自动建表（生产走 Alembic）。
    # 验收脚本用临时 SQLite，先显式建表，再启动服务。
    print("初始化临时数据库表结构...")
    init = subprocess.run(
        [
            sys.executable,
            "-X",
            "utf8",
            "-c",
            "import asyncio; from app.db.session import create_all_tables; asyncio.run(create_all_tables())",
        ],
        cwd=str(BASE_DIR),
        env=env,
        capture_output=True,
        text=True,
    )
    if init.returncode != 0:
        _fail(f"建表失败:\n{init.stdout}\n{init.stderr}")
    _ok("临时数据库建表完成")

    log_path = Path(tmp) / "server.log"
    log_file = open(log_path, "w", encoding="utf-8")
    proc = subprocess.Popen(
        [
            sys.executable,
            "-X",
            "utf8",
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(PORT),
        ],
        cwd=str(BASE_DIR),
        env=env,
        stdout=log_file,
        stderr=subprocess.STDOUT,
    )

    try:
        async with httpx.AsyncClient(timeout=120) as client:
            # 1) 服务就绪
            await _wait_ready(client, proc, log_path)
            _ok("服务启动，健康检查 200")

            # 2) 登录管理员
            r = await client.post(f"{BASE}/api/auth/login", json={"username": "admin", "password": "123456"})
            r.raise_for_status()
            headers = {"Authorization": f"Bearer {r.json()['access_token']}"}
            _ok("管理员登录成功")

            # 3) 建库 + 上传 + 等入库
            r = await client.post(f"{BASE}/api/admin/kbs", headers=headers, json={"name": "验收库"})
            r.raise_for_status()
            kb_id = r.json()["id"]
            _ok(f"知识库创建成功（kb_id={kb_id}）")

            r = await client.post(
                f"{BASE}/api/admin/kbs/{kb_id}/documents/upload",
                headers=headers,
                files={"file": ("验收文档.md", MD.encode("utf-8"), "text/markdown")},
            )
            r.raise_for_status()
            _ok("文档上传成功，等待入库...")
            await _wait_ingest(client, headers, kb_id)
            _ok("入库完成（status=ready）")

            # 4) 鉴权：无 key / 错 key
            r = await client.post(f"{BASE}/api/v1/search", json={"query": "明渠均匀流"})
            if r.status_code != 401:
                _fail(f"无 key 应 401，实际 {r.status_code}")
            _ok("无 X-API-Key → 401")

            r = await client.post(
                f"{BASE}/api/v1/search",
                json={"query": "明渠均匀流"},
                headers={"X-API-Key": "wrong"},
            )
            if r.status_code != 401:
                _fail(f"错 key 应 401，实际 {r.status_code}")
            _ok("错误 X-API-Key → 401")

            # 5) 正确 key + 真实检索
            r = await client.post(
                f"{BASE}/api/v1/search",
                json={"query": "明渠均匀流的形成条件是什么", "top_k": 5, "kb_id": kb_id},
                headers={"X-API-Key": API_KEY},
            )
            if r.status_code != 200:
                _fail(f"正确 key 应 200，实际 {r.status_code}: {r.text}")
            data = r.json()
            if not data.get("results"):
                _fail("检索结果为空（入库内容应能命中）")
            item = data["results"][0]
            # 契约字段校验
            if set(item.keys()) != {"text", "score", "source"}:
                _fail(f"契约字段不符: {sorted(item.keys())}")
            required = {
                "document_name", "document_type", "section", "page",
                "clause_no", "formula_no", "block_type", "doc_id", "chunk_id",
            }
            if set(item["source"].keys()) != required:
                _fail(f"source 契约字段不符: {sorted(item['source'].keys())}")
            if "明渠均匀流" not in item["text"]:
                _fail(f"命中内容不符: {item['text'][:80]}")
            _ok(f"正确 key → 200，命中 {len(data['results'])} 条")
            _ok(f"契约完整（text/score/source 9 字段）")
            _ok(f"命中片段: {item['text'][:60]}...")
            _ok(f"出处: {item['source']['document_name']} / {item['source']['section']}")

            # 6) 对外问答：完整回答 + 引用
            r = await client.post(
                f"{BASE}/api/v1/chat",
                json={"question": "明渠均匀流的形成条件是什么", "kb_id": kb_id, "top_k": 5},
                headers={"X-API-Key": API_KEY},
            )
            if r.status_code != 200:
                _fail(f"对外问答应 200，实际 {r.status_code}: {r.text}")
            chat = r.json()
            if set(chat.keys()) != {"answer", "citations", "evidence_level", "usage"}:
                _fail(f"问答契约字段不符: {sorted(chat.keys())}")
            if not chat.get("answer"):
                _fail("问答 answer 为空")
            if not chat.get("citations"):
                _fail("问答 citations 为空")
            if "明渠均匀流" not in chat["answer"]:
                _fail(f"问答内容不符: {chat['answer'][:80]}")
            _ok(f"对外问答 → 200，回答 {len(chat['answer'])} 字")
            _ok(f"证据等级: {chat['evidence_level']}，引用 {len(chat['citations'])} 条")
            _ok(f"回答片段: {chat['answer'][:60]}...")

            # 7) 知识库列表接口
            r = await client.get(f"{BASE}/api/v1/knowledge-bases", headers={"X-API-Key": API_KEY})
            if r.status_code != 200 or not r.json().get("items"):
                _fail(f"知识库列表失败: {r.status_code}")
            _ok(f"知识库列表 → {len(r.json()['items'])} 个库")

            print()
            print("=" * 62)
            print("验收通过：对外检索 + 问答 API 可用")
            print("  （本脚本用离线模拟模型验证链路；真实模型已在内部问答验证）")
            print("=" * 62)

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        log_file.close()
        shutil.rmtree(tmp, ignore_errors=True)
        print(f"已停止服务并清理临时目录")


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except AssertionError as exc:
        print(f"\n[FAIL] {exc}")
        sys.exit(1)
