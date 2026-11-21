"""本体数据文件（YAML）的导入/导出。

**7-A 的目标是「换一座水库只换数据文件」。** 这一层要守住三件事：

1. **表错了要整批拒绝**，不能写一半——库里既有脏数据又有缺口，还分不清是谁错了。
2. **证据只存「文件 + 页码 + 逐字引句」**，不存 chunk_id：切片 id 换一次解析器就变，
   把数据绑死在某一版解析结果上，重灌文档后全部失效。
3. **关系端点必须校验**：种子脚本当年直接写库、从不校验，于是
   `工况 --由…执行--> 单位`（工况不会执行任何动作）在图上躺了很久。
"""
from __future__ import annotations

import pytest

from app.services.graph.ontology import evidence as E
from app.services.graph.ontology.registry import load_profile
from scripts.export_ontology_data import _evidence_out
from scripts.import_ontology_data import _endpoint, validate_document


@pytest.fixture(scope="module")
def reg():
    from app.services.graph.ontology import reset_cache

    reset_cache()
    return load_profile("reservoir_flood", register=False)


class TestEvidenceRoundTrip:
    """库里的证据 → 数据文件 → 还能回找切片。"""

    def test_只导出文件页码引句(self):
        raw = [
            {
                "type": "原文",
                "doc_name": "预案.pdf",
                "chunk_id": 123,
                "printed_page": 52,
                "quote": "发生Ⅱ级险情后的处置措施",
                "note": "发生Ⅱ级险情后的处置措施",
            }
        ]
        out = _evidence_out(raw)
        assert out == [{"doc": "预案.pdf", "quote": "发生Ⅱ级险情后的处置措施", "page": 52}]
        assert "chunk_id" not in out[0], "chunk_id 不能写进数据文件"

    def test_没有文件或没有引句的证据被丢掉(self):
        assert _evidence_out([{"doc_name": "", "quote": "x"}]) == []
        assert _evidence_out([{"doc_name": "a.pdf", "quote": ""}]) == []
        assert _evidence_out(None) == []

    def test_没有印刷页码时页码字段整个不写(self):
        out = _evidence_out([{"doc_name": "a.pdf", "quote": "某句"}])
        assert out == [{"doc": "a.pdf", "quote": "某句"}]


class TestResolve:
    """`resolve`：按文件 + 引句（+ 页码）回找切片。"""

    def _index(self):
        return {
            "预案.pdf": [
                (1, E.norm("（1）高洞子水库管理处人员应在1h内传达命令"), 50),
                (2, E.norm("（1）高洞子水库管理处人员应在30min内传达命令"), 52),
                (3, E.norm("一大段父块" * 100 + "（1）高洞子水库管理处人员应在1h内传达命令"), 50),
            ]
        }

    def test_能找到就返回切片(self):
        hit = E.resolve(self._index(), "预案.pdf", "高洞子水库管理处人员应在1h内传达命令")
        assert hit is not None
        assert hit["doc_name"] == "预案.pdf"

    def test_多个候选取最短的那片(self):
        # 父块把整节都带进来，喂给模型时无关内容会把要引的那句挤掉
        hit = E.resolve(self._index(), "预案.pdf", "高洞子水库管理处人员应在1h内传达命令")
        assert hit["chunk_id"] != 3

    def test_页码用来在多个候选里缩小范围(self):
        hit = E.resolve(self._index(), "预案.pdf", "高洞子水库管理处人员应在", 52)
        assert hit is not None and hit["printed_page"] == 52

    def test_页码对不上就找不到_而不是乱指一片(self):
        assert E.resolve(self._index(), "预案.pdf", "高洞子水库管理处人员应在1h内传达命令", 99) is None

    def test_引句必须逐字出现在原文里(self):
        assert E.resolve(self._index(), "预案.pdf", "这句原文里根本没有") is None

    def test_文档不存在返回_None(self):
        assert E.resolve(self._index(), "别的.pdf", "任意") is None

    def test_空白差异不影响匹配(self):
        # MinerU 会把数字拆开：`130 m³/s` vs `1 3 0 m^3 / s`
        hit = E.resolve(
            {"a.pdf": [(9, E.norm("控制下泄流量为 1 3 0 m^3 / s"), 1)]},
            "a.pdf",
            "控制下泄流量为 130m³/s",
        )
        assert hit is not None


class TestValidateDocument:
    """整批校验：表写错了要一条都不写。"""

    def _doc(self, instances, relations):
        return {"profile_id": "reservoir_flood", "instances": instances, "relations": relations}

    def _cond(self, name="某工况", **props):
        return {"type": "condition", "name": name, "props": {"return_period": 20, **props}}

    def test_合法文档通过(self, reg):
        errors, warnings, seen = validate_document(
            reg,
            self._doc(
                [self._cond(), {"type": "response_level", "name": "Ⅳ级响应", "props": {"level": "Ⅳ"}}],
                [
                    {
                        "subject": {"type": "condition", "name": "某工况"},
                        "predicate": "TRIGGERS",
                        "object": {"type": "response_level", "name": "Ⅳ级响应"},
                    }
                ],
            ),
        )
        assert errors == []
        assert ("condition", "某工况") in seen

    def test_工况不能是执行关系的主语(self, reg):
        """真实踩过的错：`工况 —由…执行→ 单位`。工况不会执行任何动作。"""
        errors, _, _ = validate_document(
            reg,
            self._doc(
                [self._cond(), {"type": "management_unit", "name": "某局", "props": {"role": "x"}}],
                [
                    {
                        "subject": {"type": "condition", "name": "某工况"},
                        "predicate": "PERFORMED_BY",
                        "object": {"type": "management_unit", "name": "某局"},
                    }
                ],
            ),
        )
        assert any("端点非法" in e for e in errors)

    def test_方案可以挂依据_因为_tbox_声明了_source_types(self, reg):
        """依据既可能挂在动作上，也可能挂在方案上——靠 `source_types` 而不是拆关系名。"""
        assert reg.relation("CONSTRAINED_BY").allows_subject("dispatch_plan")
        assert not reg.relation("CONSTRAINED_BY").allows_subject("response_level")

    def test_未定义类型直接报错(self, reg):
        errors, _, _ = validate_document(
            reg, self._doc([{"type": "Ghost", "name": "x"}], [])
        )
        assert any("未定义实体类型" in e for e in errors)

    def test_实例重复直接报错(self, reg):
        errors, _, _ = validate_document(reg, self._doc([self._cond(), self._cond()], []))
        assert any("实例重复" in e for e in errors)

    def test_关系端点的实例必须在本文件里(self, reg):
        errors, _, _ = validate_document(
            reg,
            self._doc(
                [self._cond()],
                [
                    {
                        "subject": {"type": "condition", "name": "某工况"},
                        "predicate": "TRIGGERS",
                        "object": {"type": "response_level", "name": "不存在的等级"},
                    }
                ],
            ),
        )
        assert any("宾语实例不在本文件里" in e for e in errors)

    def test_未声明属性只是告警不拦导入(self, reg):
        errors, warnings, _ = validate_document(
            reg, self._doc([self._cond(extra_field="x")], [])
        )
        assert errors == []
        assert any("extra_field" in w for w in warnings)


class TestEndpoint:
    def test_正常解析(self):
        assert _endpoint({"type": "condition", "name": "甲"}) == ("condition", "甲")

    def test_缺字段返回_None(self):
        assert _endpoint({"type": "condition"}) is None
        assert _endpoint({"name": "甲"}) is None
        assert _endpoint("condition/甲") is None
        assert _endpoint(None) is None


class TestSourceTypes:
    """TBox 的 `source_types`：一条关系允许不止一种主语。"""

    def test_默认只允许_declared_source(self, reg):
        rel = reg.relation("PERFORMED_BY")
        assert rel.allows_subject("measure")
        assert not rel.allows_subject("condition")

    def test_allows_object_仍然只认宾语类型(self, reg):
        rel = reg.relation("CONSTRAINED_BY")
        assert rel.allows_object("standard_clause")
        assert not rel.allows_object("measure")

    def test_导出的_tbo_快照带上_source_types(self, reg):
        snap = {r["name"]: r for r in reg.to_dict()["relation_types"]}
        assert snap["CONSTRAINED_BY"]["source_types"] == ["dispatch_plan"]


class TestSyncProbe:
    """图库写入是**静默降级**的，所以同步后必须抽查——而且首尾都要验。

    实测踩过：补 7 条边，导入报告写「边 99/99」，图里一条都没有。
    原因是抽查只验第一条，而那条上次同步就在图里了，怎么都能过。
    """

    class _NoopStore:
        """写进去什么都不留——模拟图库超时降级。"""

        async def upsert(self, nodes, edges):
            return None

        async def get_node(self, uid):
            return None

        async def neighbors(self, uid, depth=1, rel_types=None, limit=200):
            class _S:
                nodes = []
                edges = []

            return _S()

    class _FullStore:
        def __init__(self):
            self.nodes = {}
            self.edges = []

        async def upsert(self, nodes, edges):
            for n in nodes:
                self.nodes[n.uid] = n
            self.edges.extend(edges)

        async def get_node(self, uid):
            return self.nodes.get(uid)

        async def neighbors(self, uid, depth=1, rel_types=None, limit=200):
            from types import SimpleNamespace

            class _S:
                pass

            s = _S()
            s.nodes = [n for u, n in self.nodes.items() if u == uid]
            s.edges = [e for e in self.edges if uid in (e.subject_uid, e.object_uid)]
            return s

    def _rows(self, n=3):
        from app.services.graph.ontology.relations import OntologyRelation

        return [
            OntologyRelation(
                profile_id="p", subject_uid=f"a:{i}", predicate="USES",
                object_uid=f"b:{i}", kb_id=0, props={}, evidence=[], source="manual",
                status="active",
            )
            for i in range(n)
        ]

    async def test_写入没生效时报0而不是报成功(self):
        from app.services.graph.ontology import relations as rel_mod

        assert await rel_mod.sync_to_graph(self._rows(), self._NoopStore()) == 0

    async def test_写入生效时报条数(self):
        from app.services.graph.ontology import relations as rel_mod

        store = self._FullStore()
        assert await rel_mod.sync_to_graph(self._rows(), store) == 3

    async def test_只有第一条写进去也算失败(self):
        """这正是实测踩到的情形：第一条早已在图里，新的整批没写进去。"""
        from app.services.graph.ontology import relations as rel_mod
        from app.services.graph.ontology.relations import OntologyRelation

        rows = self._rows(3)
        old = OntologyRelation(
            profile_id="p", subject_uid="a:0", predicate="USES", object_uid="b:0",
            kb_id=0, props={}, evidence=[], source="manual", status="active",
        )
        store = self._FullStore()
        await store.upsert([], [rel_mod.to_edge(old)])  # 第一条上次同步就在图里了

        class _Partial(self._FullStore):
            """模拟"写了一条就超时"：只留第一条。"""

            async def upsert(self, nodes, edges):
                if edges:
                    self.edges.extend(edges[:1])

        partial = _Partial()
        partial.edges = list(store.edges)
        assert await rel_mod.sync_to_graph(rows, partial) == 0
