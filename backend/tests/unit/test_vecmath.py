"""共享向量运算 vecmath 单元测试：单对余弦 + 批量余弦（numpy 向量化）。"""
from __future__ import annotations

import pytest

from app.services.vecmath import cosine, cosine_batch, score_json_batch


class TestCosine:
    def test_identical(self):
        assert cosine([1, 0, 0], [1, 0, 0]) == pytest.approx(1.0)

    def test_orthogonal(self):
        assert cosine([1, 0], [0, 1]) == pytest.approx(0.0)

    def test_opposite(self):
        assert cosine([1, 0], [-1, 0]) == pytest.approx(-1.0)

    def test_zero_vector(self):
        assert cosine([0, 0], [1, 1]) == 0.0
        assert cosine([0, 0], [0, 0]) == 0.0

    def test_dimension_mismatch_returns_zero(self):
        assert cosine([1, 0], [1]) == 0.0


class TestCosineBatch:
    def test_matches_single_cosine(self):
        query = [1.0, 0.0, 0.0]
        vectors = [
            [1.0, 0.0, 0.0],
            [0.0, 1.0, 0.0],
            [0.94, 0.3411744423496886, 0.0],
        ]
        got = cosine_batch(query, vectors)
        for g, v in zip(got, vectors):
            assert g == pytest.approx(cosine(query, v))

    def test_empty(self):
        assert cosine_batch([1, 0], []) == []

    def test_zero_query_returns_all_zero(self):
        assert cosine_batch([0, 0], [[1, 0], [0, 1]]) == [0.0, 0.0]

    def test_zero_candidate(self):
        got = cosine_batch([1, 0], [[0, 0], [1, 0]])
        assert got[0] == 0.0
        assert got[1] == pytest.approx(1.0)

    def test_preserves_order(self):
        query = [1.0, 0.0]
        vectors = [[1.0, 0.0], [0.0, 1.0], [-1.0, 0.0]]
        got = cosine_batch(query, vectors)
        assert got[0] == pytest.approx(1.0)
        assert got[1] == pytest.approx(0.0)
        assert got[2] == pytest.approx(-1.0)


class TestScoreJsonBatch:
    def test_matches_single(self):
        import json

        query = [1.0, 0.0, 0.0]
        vecs = [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]]
        jsons = [json.dumps(v) for v in vecs]
        got = score_json_batch(query, jsons)
        assert got[0] == pytest.approx(1.0)
        assert got[1] == pytest.approx(0.0)

    def test_bad_json_returns_none(self):
        got = score_json_batch([1.0, 0.0], ["[1.0, 0.0]", "not-json", "[0.0, 1.0]"])
        assert got[0] == pytest.approx(1.0)
        assert got[1] is None
        assert got[2] == pytest.approx(0.0)

    def test_empty(self):
        assert score_json_batch([1.0, 0.0], []) == []
