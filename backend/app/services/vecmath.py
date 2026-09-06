"""共享向量运算：余弦相似度（单对 + 批量 numpy 向量化）。

P1-3 事件循环阻塞修复：语义缓存 / 记忆库 / 检索补召回三处的「逐条 JSON 反序列化 +
纯 Python 三重循环余弦」是纯 CPU 计算，却跑在事件循环里，拖慢所有并发用户的首 token
延迟。这里用 numpy 把「查询向量 vs N 条候选向量」一次矩阵化算完，再由调用方放进
asyncio.to_thread 执行，主循环不再被 CPU 密集计算卡住。同时收拢了原先散落在
rag / semantic_cache / memory 三处重复的 `_cosine` 实现。
"""
from __future__ import annotations

import json

import numpy as np


def cosine(a: list[float], b: list[float]) -> float:
    """单对余弦相似度。维度不一致或任一侧零向量 → 0.0（与原实现语义一致）。"""
    av = np.asarray(a, dtype=np.float64).ravel()
    bv = np.asarray(b, dtype=np.float64).ravel()
    if av.size == 0 or bv.size == 0 or av.size != bv.size:
        return 0.0
    na = float(np.linalg.norm(av))
    nb = float(np.linalg.norm(bv))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return float(np.dot(av, bv) / (na * nb))


def cosine_batch(query: list[float], vectors: list[list[float]]) -> list[float]:
    """查询向量 vs N 条候选向量的一次性余弦（结果与逐条 cosine 一致，按输入顺序）。

    零向量 / 维度不一致的条目返回 0.0（等价原实现「跳过 / 判 0」语义，但不丢顺序）。
    """
    if not vectors:
        return []
    q = np.asarray(query, dtype=np.float64).ravel()
    nq = float(np.linalg.norm(q))
    if q.size == 0 or nq == 0.0:
        return [0.0] * len(vectors)
    try:
        mat = np.asarray(vectors, dtype=np.float64)
    except ValueError:
        # 维度不一的异常数据（不应发生）→ 退回逐条，保证健壮
        return [cosine(query, v) for v in vectors]
    if mat.ndim != 2 or mat.shape[1] != q.size:
        return [cosine(query, v) for v in vectors]
    dots = mat @ q
    norms = np.linalg.norm(mat, axis=1)
    with np.errstate(invalid="ignore", divide="ignore"):
        sims = dots / (nq * norms)
    sims = np.nan_to_num(sims, nan=0.0, posinf=0.0, neginf=0.0)
    return [float(s) for s in sims]


def score_json_batch(query: list[float], vector_jsons: list[str]) -> list[float | None]:
    """对一批 JSON 序列化的向量批量打分：反序列化 + 余弦一次做完。

    语义缓存 / 记忆库的候选池存的是 `query_vector_json` 字符串列，逐条 `json.loads`
    + 余弦是纯 CPU 密集活，跑在事件循环里会拖慢并发用户首 token。此函数把它收敛成
    一次矩阵化余弦（见 cosine_batch），供调用方放进 asyncio.to_thread 执行。
    坏 JSON 的条目返回 None（等价原「except: continue」），其余按输入顺序返回余弦。
    """
    vecs: list[list[float]] = []
    valid: list[int] = []
    for i, js in enumerate(vector_jsons):
        try:
            vecs.append(json.loads(js))
            valid.append(i)
        except (ValueError, TypeError):
            continue
    sims = cosine_batch(query, vecs)
    out: list[float | None] = [None] * len(vector_jsons)
    for idx, sim in zip(valid, sims):
        out[idx] = sim
    return out
