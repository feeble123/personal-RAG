"""P1-9 检索融合：RRF（Reciprocal Rank Fusion）+ 统一候选。

向量余弦分与 BM25 分不在同一标度（余弦 0~1、BM25 无界），线性加权会失真。
RRF 只依赖排名：score = Σ 1/(k + rank)。k=60 为经典默认。

输入：向量命中 [(chunk_id, score)] 与 BM25 命中 [(chunk_id, score)]（分数只用于排 rank）。
输出：{chunk_id: rrf_score}，融合后候选池。
"""
from __future__ import annotations

RRF_K = 60


def rrf_fuse(
    vec_hits: list[tuple[int, float]],
    bm25_hits: list[tuple[int, float]],
    k: int = RRF_K,
) -> dict[int, float]:
    """RRF 融合向量与 BM25 命中。返回 {chunk_id: rrf_score}。"""
    scores: dict[int, float] = {}
    # 向量（已按分数降序）
    for rank, (cid, _) in enumerate(vec_hits, start=1):
        scores[cid] = scores.get(cid, 0.0) + 1.0 / (k + rank)
    # BM25（已按分数降序）
    for rank, (cid, _) in enumerate(bm25_hits, start=1):
        scores[cid] = scores.get(cid, 0.0) + 1.0 / (k + rank)
    return scores


def rrf_score_to_prob(rrf: float, hits_count: int) -> float:
    """RRF 分 → 0~1 归一化（供 trace/evidence 参考；不是概率）。"""
    if hits_count == 0:
        return 0.0
    return min(1.0, rrf / (2.0 * hits_count))
