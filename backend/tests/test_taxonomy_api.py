"""单元 2：知识分类体系 —— 接口层测试。

覆盖：
- 知识库创建/修改时带业务域（含非法值回退）
- 知识库列表按业务域筛选
- 分类字典接口
- 文档列表按文档类型筛选
- 检索按业务域 / 文档类型过滤（真实入库 + fake 模型，走 HTTP）
"""
from __future__ import annotations


class TestKbDomain:
    async def test_create_with_domain(self, client, admin_headers):
        r = await client.post(
            "/api/admin/kbs",
            headers=admin_headers,
            json={"name": "分类测试-防洪", "business_domain": "flood"},
        )
        assert r.status_code == 201, r.text
        data = r.json()
        assert data["business_domain"] == "flood"
        await client.delete(f"/api/admin/kbs/{data['id']}", headers=admin_headers)

    async def test_create_without_domain_defaults_general(self, client, admin_headers):
        r = await client.post(
            "/api/admin/kbs", headers=admin_headers, json={"name": "分类测试-默认"}
        )
        assert r.status_code == 201
        assert r.json()["business_domain"] == "general"
        await client.delete(f"/api/admin/kbs/{r.json()['id']}", headers=admin_headers)

    async def test_create_with_invalid_domain_falls_back(self, client, admin_headers):
        r = await client.post(
            "/api/admin/kbs",
            headers=admin_headers,
            json={"name": "分类测试-非法", "business_domain": "不存在的域"},
        )
        assert r.status_code == 201
        assert r.json()["business_domain"] == "general"
        await client.delete(f"/api/admin/kbs/{r.json()['id']}", headers=admin_headers)

    async def test_update_domain(self, client, admin_headers):
        r = await client.post(
            "/api/admin/kbs", headers=admin_headers, json={"name": "分类测试-改域"}
        )
        kb_id = r.json()["id"]
        r = await client.patch(
            f"/api/admin/kbs/{kb_id}",
            headers=admin_headers,
            json={"business_domain": "water"},
        )
        assert r.status_code == 200
        assert r.json()["business_domain"] == "water"
        await client.delete(f"/api/admin/kbs/{kb_id}", headers=admin_headers)

    async def test_list_filter_by_domain(self, client, admin_headers):
        created = []
        for name, domain in (("筛选测试-防洪", "flood"), ("筛选测试-建设", "construction")):
            r = await client.post(
                "/api/admin/kbs",
                headers=admin_headers,
                json={"name": name, "business_domain": domain},
            )
            created.append(r.json()["id"])

        try:
            r = await client.get(
                "/api/admin/kbs", headers=admin_headers, params={"business_domain": "flood"}
            )
            assert r.status_code == 200
            names = {kb["name"] for kb in r.json()}
            assert "筛选测试-防洪" in names
            assert "筛选测试-建设" not in names
            # 返回的每一行都必须是该域
            assert all(kb["business_domain"] == "flood" for kb in r.json())
        finally:
            for kb_id in created:
                await client.delete(f"/api/admin/kbs/{kb_id}", headers=admin_headers)


class TestTaxonomyEndpoint:
    async def test_options(self, client, user_headers):
        r = await client.get("/api/knowledge-bases/taxonomy", headers=user_headers)
        assert r.status_code == 200
        data = r.json()
        assert len(data["business_domains"]) == 6
        assert len(data["doc_types"]) == 10
        assert any(
            d["value"] == "flood" and d["label"] == "防洪减灾" for d in data["business_domains"]
        )


class TestDocumentTypeFilter:
    async def test_filter_documents(self, client, admin_headers, sample_kb):
        kb_id, _doc_id = sample_kb
        r = await client.get(
            f"/api/admin/kbs/{kb_id}/documents", headers=admin_headers, params={"doc_type": "other"}
        )
        assert r.status_code == 200
        assert r.json()["total"] == 1

        r = await client.get(
            f"/api/admin/kbs/{kb_id}/documents", headers=admin_headers, params={"doc_type": "standard"}
        )
        assert r.status_code == 200
        assert r.json()["total"] == 0


class TestScopedRetrieval:
    """检索按业务域 / 文档类型过滤（真实入库，fake embedding）。"""

    async def _set_domain(self, client, admin_headers, kb_id: int, domain: str) -> None:
        r = await client.patch(
            f"/api/admin/kbs/{kb_id}", headers=admin_headers, json={"business_domain": domain}
        )
        assert r.status_code == 200

    async def test_domain_match_returns_hits(self, client, admin_headers, sample_kb):
        kb_id, _doc_id = sample_kb
        await self._set_domain(client, admin_headers, kb_id, "flood")

        r = await client.get(
            "/api/admin/search",
            headers=admin_headers,
            params={"q": "明渠均匀流的形成条件", "kb_id": kb_id, "business_domain": "flood"},
        )
        assert r.status_code == 200, r.text
        assert len(r.json()["hits"]) > 0

    async def test_domain_mismatch_returns_empty(self, client, admin_headers, sample_kb):
        """文档属于 flood，却按 water 检索 → 空结果（不串域）。"""
        kb_id, _doc_id = sample_kb
        await self._set_domain(client, admin_headers, kb_id, "flood")

        r = await client.get(
            "/api/admin/search",
            headers=admin_headers,
            params={"q": "明渠均匀流的形成条件", "kb_id": kb_id, "business_domain": "water"},
        )
        assert r.status_code == 200
        assert r.json()["hits"] == []

    async def test_doc_type_mismatch_returns_empty(self, client, admin_headers, sample_kb):
        kb_id, _doc_id = sample_kb
        r = await client.get(
            "/api/admin/search",
            headers=admin_headers,
            params={"q": "明渠均匀流的形成条件", "kb_id": kb_id, "doc_type": "emergency_plan"},
        )
        assert r.status_code == 200
        assert r.json()["hits"] == []

    async def test_no_filter_returns_hits(self, client, admin_headers, sample_kb):
        kb_id, _doc_id = sample_kb
        r = await client.get(
            "/api/admin/search",
            headers=admin_headers,
            params={"q": "明渠均匀流的形成条件", "kb_id": kb_id},
        )
        assert r.status_code == 200
        assert len(r.json()["hits"]) > 0
