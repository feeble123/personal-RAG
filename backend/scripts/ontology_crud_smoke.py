"""本体 CRUD 冒烟：真的建一个节点、一条关系，再改、再删（自带清理）。

验证的是管理端接口在**真实数据库**上的完整闭环：
    建实例 → 建关系 → 图谱里能看到 → 改关系 → 删实例（关系级联删除）

用法（在 backend 目录下，服务需已启动）：
    python -m scripts.ontology_crud_smoke
    python -m scripts.ontology_crud_smoke --base http://127.0.0.1:8000

注意：需要管理员账号。用户名密码从环境变量读，不写死在脚本里：
    RAG_ADMIN_USER（默认 admin）/ RAG_ADMIN_PASSWORD
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

NAME_A = "smoke_param_a"
NAME_B = "smoke_param_b"


def _fail(step: str, resp) -> int:
    print(f"❌ {step} 失败：HTTP {resp.status_code} {resp.text[:300]}")
    return 1


def main() -> int:
    import httpx

    parser = argparse.ArgumentParser(description="本体 CRUD 冒烟")
    parser.add_argument("--base", default="http://127.0.0.1:8000")
    args = parser.parse_args()

    user = os.environ.get("RAG_ADMIN_USER", "admin")
    password = os.environ.get("RAG_ADMIN_PASSWORD")
    if not password:
        print("缺少环境变量 RAG_ADMIN_PASSWORD")
        return 1

    api = args.base.rstrip("/")
    with httpx.Client(timeout=30) as client:
        r = client.post(f"{api}/api/auth/login", json={"username": user, "password": password})
        if r.status_code != 200:
            return _fail("登录", r)
        headers = {"Authorization": f"Bearer {r.json()['access_token']}"}
        print("✓ 登录")

        # 1. 建两个实例（重名先删，保证可重复执行）
        created = []
        for name, display in ((NAME_A, "冒烟参数A"), (NAME_B, "冒烟参数B")):
            existing = client.get(
                f"{api}/api/admin/ontology/instances",
                headers=headers,
                params={"limit": 1000, "status": "all"},
            ).json()
            for row in existing:
                if row["name"] == name:
                    client.delete(
                        f"{api}/api/admin/ontology/instances/{row['id']}", headers=headers
                    )
            r = client.post(
                f"{api}/api/admin/ontology/instances",
                headers=headers,
                json={
                    "entity_type": "Parameter",
                    "name": name,
                    "props": {"display_zh": display, "group": "产流", "unit": "mm"},
                    "evidence": [{"type": "手册", "note": "冒烟测试用"}],
                },
            )
            if r.status_code != 201:
                return _fail(f"建实例 {name}", r)
            created.append(r.json())
            print(f"✓ 建实例 {name} → {r.json()['uid']}")

        uid_a, uid_b = created[0]["uid"], created[1]["uid"]
        id_a = created[0]["id"]

        # 2. 建关系
        r = client.post(
            f"{api}/api/admin/ontology/relations",
            headers=headers,
            json={
                "subject_uid": uid_a,
                "predicate": "PART_OF",
                "object_uid": uid_b,
                "evidence": [{"type": "人工", "note": "冒烟关系"}],
            },
        )
        if r.status_code != 201:
            return _fail("建关系", r)
        relation_id = r.json()["id"]
        print(f"✓ 建关系 PART_OF (id={relation_id})")

        # 3. 图谱里能看到这条边（节点 + 边一次性拿到）
        graph = client.get(f"{api}/api/admin/ontology/graph", headers=headers).json()
        uids = {n["uid"] for n in graph["nodes"]}
        has_edge = any(
            e["source"] == uid_a and e["target"] == uid_b and e["predicate"] == "PART_OF"
            for e in graph["edges"]
        )
        if not (uid_a in uids and uid_b in uids and has_edge):
            print("❌ 图谱里没找到刚建的节点/关系")
            return 1
        print(f"✓ 图谱可见（{len(graph['nodes'])} 节点 / {len(graph['edges'])} 关系）")

        # 4. 改关系
        r = client.patch(
            f"{api}/api/admin/ontology/relations/{relation_id}",
            headers=headers,
            json={"evidence": [{"type": "教材", "note": "追加的依据"}]},
        )
        if r.status_code != 200:
            return _fail("改关系", r)
        notes = [e.get("note") for e in (r.json().get("evidence") or [])]
        if "冒烟关系" not in notes or "追加的依据" not in notes:
            print(f"❌ 证据没有并集：{notes}")
            return 1
        print("✓ 改关系（证据只增不减）")

        # 5. 删实例 → 关系级联删除
        r = client.delete(f"{api}/api/admin/ontology/instances/{id_a}", headers=headers)
        if r.status_code != 204:
            return _fail("删实例", r)
        left = client.get(
            f"{api}/api/admin/ontology/relations",
            headers=headers,
            params={"touch_uid": uid_a, "status": "all"},
        ).json()
        if left:
            print(f"❌ 实例删了但关系还在：{left}")
            return 1
        print("✓ 删实例，关系已级联删除")

        # 收尾：删掉另一个实例
        for row in client.get(
            f"{api}/api/admin/ontology/instances", headers=headers,
            params={"limit": 1000, "status": "all"},
        ).json():
            if row["name"] == NAME_B:
                client.delete(f"{api}/api/admin/ontology/instances/{row['id']}", headers=headers)
        print("\n全部通过 ✅（测试数据已清理）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
