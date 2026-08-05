"""全量重灌：解析/分块规则变更后，通过运行中的后端 API 重新入库所有文档。

用法（backend 目录）：
    PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe scripts/reingest_all.py

前提：后端服务已启动（uvicorn --reload 已加载新解析/分块代码），管理员账号可用。
说明：
- 逐个调用 /admin/documents/{id}/reparse 重新排队，轮询等待全部完成；
- 扫描版 PDF 会重跑 OCR（54 页约 6 分钟），期间前端可实时看到进度；
- 仅重灌 ready/failed 状态文档；入库中的跳过。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import httpx

BASE = "http://localhost:8000"
USERNAME = "admin"
PASSWORD = "123456"


def main() -> None:
    with httpx.Client(base_url=BASE, timeout=60) as client:
        r = client.post("/api/auth/login", json={"username": USERNAME, "password": PASSWORD})
        r.raise_for_status()
        token = r.json()["access_token"]
        h = {"Authorization": f"Bearer {token}"}

        kbs = client.get("/api/admin/kbs", headers=h).json()
        docs: list[tuple[str, dict]] = []
        for kb in kbs:
            r = client.get(
                f"/api/admin/kbs/{kb['id']}/documents", headers=h, params={"page_size": 100}
            )
            for d in r.json()["items"]:
                docs.append((kb["name"], d))
        print(f"共 {len(docs)} 个文档")

        to_reparse = [x for x in docs if x[1]["status"] in ("ready", "failed")]
        for kb, d in docs:
            if d["status"] not in ("ready", "failed"):
                print(f"跳过（入库中）: [{kb}] {d['filename']} ({d['status']})")

        for kb, d in to_reparse:
            print(f"重新排队: [{kb}] {d['filename']}")
            client.post(f"/api/admin/documents/{d['id']}/reparse", headers=h).raise_for_status()

        # 轮询等待全部完成
        print("等待入库完成…")
        deadline = time.time() + 60 * 60
        while time.time() < deadline:
            pending = []
            for _kb, d in to_reparse:
                st = client.get(f"/api/admin/documents/{d['id']}", headers=h).json()["status"]
                if st in ("pending", "parsing", "embedding"):
                    pending.append((d["filename"], st))
            if not pending:
                break
            print("  进行中:", ", ".join(f"{f}({s})" for f, s in pending[:6]))
            time.sleep(5)

        print("\n== 结果 ==")
        for kb, d in to_reparse:
            doc = client.get(f"/api/admin/documents/{d['id']}", headers=h).json()
            print(
                f"[{kb}] {doc['filename']} | {doc['status']} | "
                f"chunks={doc['chunk_count']} | 页数={doc['page_count']}"
            )


if __name__ == "__main__":
    main()
