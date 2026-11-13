"""本体问答试验：直接看「图谱事实 + 知识库原文」组合出来的结果（只读）。

用法（在 backend 目录下，服务已启动）：
    RAG_ADMIN_PASSWORD=xxx python -m scripts.ontology_ask_demo
    RAG_ADMIN_PASSWORD=xxx python -m scripts.ontology_ask_demo "sm 取多少"

输出分三块，缺一不可：
    · 来源    —— ontology（参数卡/图谱）/ graph / rag，以及是否回退
    · 事实    —— 每条事实的结构化内容
    · 依据    —— 能不能点回文档的哪一页/哪个切片
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

DEFAULT_QUESTIONS = [
    "sm 这个参数一般取多少？依据是什么？",       # 应命中本体参数卡（双证据）
    "自由水蓄水能力应该怎么取值",                 # 中文名，考实体链接
    "青溪水库下游有哪些乡镇需要预警",             # 考图谱关系
    "蒸散发系数 ke 的取值范围",                   # 另一个参数，看是否稳定
    "混凝土浇筑温度要求",                         # 本体没有 → 应回退向量检索
]

KIND = {
    "parameter_prior": "参数先验",
    "graph_node": "图谱节点",
    "graph_edge": "图谱关系",
    "text_chunk": "知识库原文",
}


def _print_pack(data: dict) -> None:
    print(
        f"  来源={data['sources'] or ['—']} 回退={data['fallback_used']} "
        f"事实={len(data['facts'])} 耗时={data['latency_ms']}ms"
    )
    for note in data.get("notes") or []:
        print(f"  · 提示：{note}")
    for fact in data["facts"][:5]:
        kind = KIND.get(str(fact.get("kind")), str(fact.get("kind")))
        if fact.get("kind") == "parameter_prior":
            print(
                f"  · [{kind}] {fact.get('subject')} 范围={fact.get('range')} "
                f"初值={fact.get('initial')} 证据等级={fact.get('evidence_level')}"
            )
        elif fact.get("kind") == "graph_edge":
            rel = fact.get("predicate_label") or fact.get("predicate")
            print(f"  · [{kind}] {fact.get('subject')} -{rel}-> {fact.get('object')}")
        elif fact.get("kind") == "graph_node":
            etype = fact.get("entity_type_label") or fact.get("entity_type")
            print(f"  · [{kind}] {fact.get('name')}（{etype}）")
        else:
            print(f"  · [{kind}] 第{fact.get('page')}页：{str(fact.get('snippet'))[:70]}")
        for ev in (fact.get("evidence") or [])[:2]:
            if not isinstance(ev, dict):
                continue
            print(
                f"      依据：{ev.get('type') or '—'} "
                f"{ev.get('doc_name') or ''} doc={ev.get('doc_id')} "
                f"chunk={ev.get('chunk_id')} 第{ev.get('page')}页"
            )


def main() -> int:
    import httpx

    parser = argparse.ArgumentParser(description="本体问答试验")
    parser.add_argument("question", nargs="*", help="要问的问题；不传则跑一组默认问题")
    parser.add_argument("--base", default="http://127.0.0.1:8000")
    args = parser.parse_args()

    user = os.environ.get("RAG_ADMIN_USER", "admin")
    password = os.environ.get("RAG_ADMIN_PASSWORD")
    if not password:
        print("缺少环境变量 RAG_ADMIN_PASSWORD")
        return 1

    questions = args.question or DEFAULT_QUESTIONS
    api = args.base.rstrip("/")
    with httpx.Client(timeout=120) as client:
        r = client.post(f"{api}/api/auth/login", json={"username": user, "password": password})
        if r.status_code != 200:
            print(f"登录失败：{r.status_code} {r.text[:200]}")
            return 1
        headers = {"Authorization": f"Bearer {r.json()['access_token']}"}

        for q in questions:
            print("=" * 74)
            print(f"问：{q}")
            resp = client.post(
                f"{api}/api/admin/ontology/ask",
                headers=headers,
                json={"question": q, "min_facts": 1},
            )
            if resp.status_code != 200:
                print(f"  失败：HTTP {resp.status_code} {resp.text[:200]}")
                continue
            _print_pack(resp.json())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
