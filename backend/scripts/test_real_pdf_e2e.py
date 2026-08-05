"""真实 PDF 端到端测试：上传扫描版 PDF → OCR 入库 → 检索 → 引用问答。

用法（需先启动后端，或用 FAKE 模式）：
    PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe scripts/test_real_pdf_e2e.py "<pdf路径>"
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import httpx

BASE_DIR = Path(__file__).resolve().parents[1]

BASE = "http://localhost:8012"


def main() -> None:
    if len(sys.argv) < 2:
        print("用法: python scripts/test_real_pdf_e2e.py <pdf路径>")
        return
    pdf = Path(sys.argv[1])
    if not pdf.exists():
        print(f"文件不存在: {pdf}")
        return

    c = httpx.Client(base_url=BASE, timeout=60)
    try:
        # 1) admin 登录
        r = c.post("/api/auth/login", json={"username": "admin", "password": "123456"})
        H = {"Authorization": f"Bearer {r.json()['access_token']}"}

        # 2) 建库（幂等：同名跳过）
        r = c.get("/api/admin/kbs", headers=H)
        kb_id = next((k["id"] for k in r.json() if k["name"] == "真实PDF测试库"), None)
        if not kb_id:
            r = c.post("/api/admin/kbs", headers=H, json={"name": "真实PDF测试库"})
            kb_id = r.json()["id"]
        print(f"[1] 知识库就绪 id={kb_id}")

        # 3) 上传真实 PDF
        with open(pdf, "rb") as f:
            r = c.post(
                f"/api/admin/kbs/{kb_id}/documents/upload",
                headers=H,
                files={"file": (pdf.name, f, "application/pdf")},
            )
        doc_id = r.json()["id"]
        print(f"[2] 上传成功 doc_id={doc_id}，开始 OCR 入库（扫描版可能需要数分钟）…")

        # 4) 轮询入库状态
        t0 = time.time()
        status = "pending"
        while time.time() - t0 < 480:
            r = c.get(f"/api/admin/kbs/{kb_id}/documents", headers=H)
            for d in r.json()["items"]:
                if d["id"] == doc_id:
                    status = d["status"]
                    break
            if status in ("ready", "failed"):
                break
            time.sleep(5)
        el = time.time() - t0
        detail = next(d for d in c.get(f"/api/admin/kbs/{kb_id}/documents", headers=H).json()["items"] if d["id"] == doc_id)
        print(f"[3] 入库状态: {status}（耗时 {el:.0f}s）| chunks={detail['chunk_count']} | 质量={detail['quality']}")
        if status != "ready":
            print(f"    失败原因: {detail['error_message']}")
            return

        # 5) 检索测试（真实库中搜 数字孪生）
        for q in ["数字孪生水利工程的定义", "水利工程数据底板", "防洪调度"]:
            r = c.get("/api/admin/search", headers=H, params={"q": q, "kb_id": kb_id, "top_k": 2})
            hits = r.json()["hits"]
            print(f"[4] 检索「{q}」→ {len(hits)} 条")
            for h in hits[:2]:
                print(f"      #{h['rank']} {h['source']} 页{h['page']} 相关度{h['score']:.3f}")
                print(f"        片段: {h['snippet'][:70]}…")
    finally:
        c.close()


if __name__ == "__main__":
    main()
