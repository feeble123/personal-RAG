"""F5 图提案：模型提新实例和新关系，程序只认「有出处、合本体」的那些。

这里守的六条闸，每条都对应一种真实的坏数据：

1. 编造的引用 → 假出处比没出处更坏，人拿它当依据去建模；
2. 自创的类型/关系 → 本体就不再是本体，只是一个自由文本图；
3. **端点写反** → 我们自己手工建模时就被校验器抓出过 5 处，模型只会更多；
4. 悬空的关系 → 关系连到一个根本没声明的名字；
5. 重复实例 → 第二个「Ⅱ级响应」和第一个不是一回事；
6. 凭「记忆」填的值 → 值不在引句里，就是从别处编来的。
"""
from __future__ import annotations

from types import SimpleNamespace

from app.services.graph.ontology import proposal_prompts, proposals
from app.services.graph.ontology.anchoring import Candidate
from app.services.graph.ontology.registry import (
    AttributeDef,
    EntityTypeDef,
    OntologyRegistry,
    RelationTypeDef,
)

CONTENT = (
    "4.6.1.2 Ⅱ级响应\n"
    "当库水位达到 1012.50 m 时，启动Ⅱ级响应，由防汛办通知安澜镇人民政府组织转移。\n"
    "Ⅱ级响应应采取以下措施：加强水情监测、开启泄洪闸控泄。\n"
)


def _registry() -> OntologyRegistry:
    reg = OntologyRegistry(
        profile_id="test_flood",
        version=1,
        domain="flood",
        entity_types={
            "response_level": EntityTypeDef(
                name="response_level",
                display_name="响应等级",
                uid_prefix="rl",
                scope="global",
                label="ResponseLevel",
                attributes={
                    "level": AttributeDef(
                        name="level",
                        type="enum",
                        required=True,
                        values=("Ⅰ", "Ⅱ", "Ⅲ"),
                        description="响应级别",
                    ),
                    "trigger": AttributeDef(name="trigger", type="number", description="触发水位（m）"),
                },
            ),
            "measure": EntityTypeDef(
                name="measure",
                display_name="处置措施",
                uid_prefix="ms",
                scope="local",
                label="Measure",
                attributes={
                    "source_item": AttributeDef(
                        name="source_item", type="number", description="原文第几条"
                    )
                },
            ),
            "standard_clause": EntityTypeDef(
                name="standard_clause",
                display_name="标准条款",
                uid_prefix="cl",
                scope="global",
                label="Clause",
                attributes={
                    "doc": AttributeDef(name="doc", type="string", description="出自哪份文档"),
                    "clause_no": AttributeDef(name="clause_no", type="string", description="条款号"),
                },
            ),
            "management_unit": EntityTypeDef(
                name="management_unit",
                display_name="责任单位",
                uid_prefix="mu",
                scope="global",
                label="Unit",
                attributes={},
            ),
        },
        relation_types={
            "RESPONDS_WITH": RelationTypeDef(
                name="RESPONDS_WITH",
                source="response_level",
                target="measure",
                display_name="以这些措施响应",
            ),
            "PERFORMED_BY": RelationTypeDef(
                name="PERFORMED_BY",
                source="measure",
                target="management_unit",
                display_name="由谁执行",
            ),
        },
    )
    reg.register()
    return reg


def _chunks() -> list[Candidate]:
    return [
        Candidate(
            chunk_id=1,
            doc_id=9,
            doc_name="调度规程.pdf",
            page=42,
            section=None,
            content=CONTENT,
            printed_page=39,
        )
    ]


def _known():
    return proposals.known_from_instances(
        [
            SimpleNamespace(uid="mu:1", entity_type="management_unit", name="安澜镇人民政府", props={}),
            SimpleNamespace(uid="rl:1", entity_type="response_level", name="Ⅰ级响应", props={}),
        ]
    )


def _run(items, relations=(), *, kb_id=0):
    by_name, by_uid, _ = _known()
    result = proposals.ProposalRunResult()
    instances, rels = proposals.build_candidates(
        list(items),
        list(relations),
        _chunks(),
        registry=_registry(),
        by_name=by_name,
        by_uid=by_uid,
        kb_id=kb_id,
        result=result,
    )
    return instances, rels, result


def _level_item(**over):
    item = {
        "type": "response_level",
        "name": "Ⅱ级响应",
        "props": {"level": "Ⅱ", "trigger": 1012.5},
        "chunk": 1,
        "quote": "当库水位达到 1012.50 m 时，启动Ⅱ级响应",
    }
    item.update(over)
    return item


def _measure_item(name="加强水情监测", **over):
    item = {
        "type": "measure",
        "name": name,
        "props": {"source_item": 1},
        "chunk": 1,
        "quote": "Ⅱ级响应应采取以下措施：加强水情监测",
    }
    item.update(over)
    return item


class TestTBoxDigest:
    def test_类型和字段说明都给到模型(self):
        """模型不知道某个类型有哪些字段，就只能瞎猜——摘要写得清，提案才准。"""
        text = proposal_prompts.tbox_digest(_registry())
        assert "response_level（响应等级）" in text
        assert "枚举: Ⅰ/Ⅱ/Ⅲ" in text
        assert "触发水位" in text

    def test_关系的端点必须写清(self):
        text = proposal_prompts.tbox_digest(_registry())
        assert "RESPONDS_WITH（以这些措施响应）：response_level → measure" in text


class TestParsePayload:
    def test_从代码块里抠出JSON(self):
        text = '```json\n{"instances":[{"type":"a"}],"relations":[{"predicate":"b"}]}\n```'
        insts, rels = proposal_prompts.parse_payload(text)
        assert insts == [{"type": "a"}]
        assert rels == [{"predicate": "b"}]

    def test_乱七八糟的输出不炸(self):
        assert proposal_prompts.parse_payload("我看了一下，没找到东西。") == ([], [])


class TestInstanceCandidate:
    def test_合本体的实例能过并生成确定性uid(self):
        instances, _, _ = _run([_level_item()])
        assert len(instances) == 1
        cand = instances[0]
        assert cand.entity_type == "response_level"
        assert cand.props["level"] == "Ⅱ"
        assert cand.props["trigger"] == 1012.5
        assert cand.uid.startswith("rl:")

    def test_自创的类型被拒(self):
        """模型自创概念是本体塌向 GraphRAG 的第一步。"""
        _, _, result = _run([_level_item(type="flood_event")])
        assert result.reject_reasons["unknown_type"] == 1

    def test_一串并列的实体不算一个实例(self):
        """实测真实模型把「镇人民政府、街道办事处、区政府各部门」当成**一个**
        责任单位。建出来的是个查不到、也挂不上关系的假实体——宁可拒掉让人拆。"""
        _, _, result = _run(
            [
                _level_item(
                    name="镇人民政府、街道办事处、区政府各部门",
                )
            ]
        )
        assert result.reject_reasons["list_name"] == 1
        assert any("像一串并列的实体" in n for n in result.notes)

    def test_名字里的和与及不算并列(self):
        """「和平镇」是正常地名，拿「和」判断会误伤。"""
        instances, _, _ = _run([_level_item(name="和平镇人民政府")])
        assert len(instances) == 1

    def test_编造的引用被拒(self):
        """假出处比没出处更坏——用户会拿它当依据去建模。"""
        _, _, result = _run([_level_item(quote="当库水位达到 1012.50 m 时，全市进入紧急状态")])
        assert result.reject_reasons["quote_not_in_chunk"] == 1

    def test_已有同名实例不重复新建(self):
        _, _, result = _run([_level_item(name="Ⅰ级响应")])
        assert result.reject_reasons["duplicate_instance"] == 1

    def test_同批重复也只留一条(self):
        instances, _, _ = _run([_level_item(), _level_item()])
        assert len(instances) == 1

    def test_缺必填字段只提醒不拒(self):
        """提案可以不全——缺什么由人补；真正写进库时才拦（accept 那一关）。"""
        instances, _, result = _run([_level_item(props={"trigger": 1012.5})])
        assert result.rejected == 0
        assert any("缺必填字段 level" in w for w in instances[0].warnings)

    def test_值不在引句里就丢掉(self):
        """值必须出现在那句引用里，否则就是从别处编来的。"""
        instances, _, _ = _run([_level_item(props={"level": "Ⅱ", "trigger": 1200})])
        assert "trigger" not in instances[0].props
        assert any("trigger" in w for w in instances[0].warnings)

    def test_文件名可以不出现在引句里(self):
        """`standard_clause.doc` 填的就是文件名，引句是条款正文、永远不含文件名。

        切片来自哪份文档是**系统确知**的事实，不需要靠引句证明；不加这条例外，
        每个条款实例都会莫名其妙少一个字段（真实数据 9 个条款全是这么填的）。
        """
        instances, _, _ = _run(
            [
                _level_item(
                    props={"level": "Ⅱ", "doc": "调度规程.pdf"},
                )
            ]
        )
        assert "doc" not in instances[0].props  # 该类型没声明 doc，照样丢

        # 声明了 doc 的类型：来源文件名放行，别的文件名不放行
        instances, _, _ = _run(
            [
                {
                    "type": "standard_clause",
                    "name": "调度规程 4.6.1.2",
                    "props": {"doc": "调度规程.pdf", "clause_no": "4.6.1.2"},
                    "chunk": 1,
                    "quote": "4.6.1.2 Ⅱ级响应",
                }
            ]
        )
        assert instances[0].props["doc"] == "调度规程.pdf"

    def test_别的文件名照样要出现在引句里(self):
        instances, _, _ = _run(
            [
                {
                    "type": "standard_clause",
                    "name": "调度规程 4.6.1.2",
                    "props": {"doc": "完全无关的另一份文件.pdf"},
                    "chunk": 1,
                    "quote": "4.6.1.2 Ⅱ级响应",
                }
            ]
        )
        assert "doc" not in instances[0].props
        assert any("doc" in w for w in instances[0].warnings)

    def test_TBox里没声明的字段丢掉(self):
        instances, _, _ = _run([_level_item(props={"level": "Ⅱ", "color": "红"})])
        assert "color" not in instances[0].props


class TestRelationCandidate:
    def test_同批新实例能被关系连上(self):
        """先建实例再连边——这个顺序是硬要求，否则关系只能连到旧数据。"""
        instances, relations, _ = _run(
            [_level_item(), _measure_item()],
            [
                {
                    "subject": "Ⅱ级响应",
                    "predicate": "RESPONDS_WITH",
                    "object": "加强水情监测",
                    "chunk": 1,
                    "quote": "Ⅱ级响应应采取以下措施：加强水情监测",
                }
            ],
        )
        assert len(instances) == 2
        assert len(relations) == 1
        assert relations[0].subject_uid == instances[0].uid
        assert relations[0].object_uid == instances[1].uid

    def test_能连到库里已有实例(self):
        _, relations, _ = _run(
            [_measure_item()],
            [
                {
                    "subject": "加强水情监测",
                    "predicate": "PERFORMED_BY",
                    "object": "安澜镇人民政府",
                    "chunk": 1,
                    "quote": "由防汛办通知安澜镇人民政府组织转移",
                }
            ],
        )
        assert relations[0].object_uid == "mu:1"

    def test_端点写反了要单独报出来(self):
        """这是我们自己建模时犯得最多的错（校验器抓过 5 处）；
        只说「端点非法」，人还得自己猜该改哪头——说「写反了」就能直接改。"""
        _, relations, result = _run(
            [_level_item(), _measure_item()],
            [
                {
                    "subject": "加强水情监测",
                    "predicate": "RESPONDS_WITH",
                    "object": "Ⅱ级响应",
                    "chunk": 1,
                    "quote": "Ⅱ级响应应采取以下措施：加强水情监测",
                }
            ],
        )
        assert relations == []
        assert result.reject_reasons["reversed_endpoint"] == 1

    def test_端点根本不搭的另一种错分开报(self):
        _, _, result = _run(
            [_level_item(), _measure_item()],
            [
                {
                    "subject": "Ⅱ级响应",
                    "predicate": "PERFORMED_BY",
                    "object": "加强水情监测",
                    "chunk": 1,
                    "quote": "Ⅱ级响应应采取以下措施：加强水情监测",
                }
            ],
        )
        assert result.reject_reasons["bad_endpoint"] == 1

    def test_自创的关系被拒(self):
        _, _, result = _run(
            [_level_item(), _measure_item()],
            [
                {
                    "subject": "Ⅱ级响应",
                    "predicate": "RELATED_TO",
                    "object": "加强水情监测",
                    "chunk": 1,
                    "quote": "Ⅱ级响应应采取以下措施：加强水情监测",
                }
            ],
        )
        assert result.reject_reasons["unknown_relation"] == 1

    def test_提到没给出的实例就悬空了_拒掉(self):
        """宁可少连一条边，也不要连到一个不存在的东西上。"""
        _, relations, result = _run(
            [_level_item()],
            [
                {
                    "subject": "Ⅱ级响应",
                    "predicate": "RESPONDS_WITH",
                    "object": "开启泄洪闸控泄",
                    "chunk": 1,
                    "quote": "开启泄洪闸控泄",
                }
            ],
        )
        assert relations == []
        assert result.reject_reasons["unknown_ref"] == 1

    def test_连到被拒实例的关系_报同一个根因(self):
        """一个实例没通过校验，引用它的每条边都会「找不到」——那是同一个根因。

        分开报会让人以为模型编了好几个不存在的东西，去查的方向就错了。
        """
        _, relations, result = _run(
            [_level_item(), _measure_item(quote="这句原文里没有")],
            [
                {
                    "subject": "Ⅱ级响应",
                    "predicate": "RESPONDS_WITH",
                    "object": "加强水情监测",
                    "chunk": 1,
                    "quote": "Ⅱ级响应应采取以下措施：加强水情监测",
                }
            ],
        )
        assert relations == []
        assert result.reject_reasons.get("ref_rejected") == 1
        assert "unknown_ref" not in result.reject_reasons
        assert any("自己没通过校验" in n for n in result.notes)

    def test_不要求引句里出现两端名字(self):
        """**刻意不加的校验**，有实测依据。

        我们自己的 92 条人工核实过的关系里，两端名字都在引句里的 0 条、
        一个都不在的 72 条。中文技术文档先给实体、正文里说规则，证据句承载的是
        关系的内容（泄量、阈值），不重复点名。硬要求名字出现会拒掉 78% 的
        正确数据，还会逼模型编造假引文——比不加校验更坏。
        """
        _, relations, _ = _run(
            [
                {
                    "type": "response_level",
                    "name": "Ⅱ级响应",
                    "props": {"level": "Ⅱ"},
                    "chunk": 1,
                    "quote": "当库水位达到 1012.50 m 时，启动Ⅱ级响应",
                },
                {
                    "type": "measure",
                    "name": "加强水情监测",
                    "props": {},
                    "chunk": 1,
                    "quote": "加强水情监测",
                },
            ],
            [
                {
                    "subject": "Ⅱ级响应",
                    "predicate": "RESPONDS_WITH",
                    "object": "加强水情监测",
                    "chunk": 1,
                    "quote": "当库水位达到 1012.50 m 时",  # 两端都没提到，但这是真引文
                }
            ],
        )
        # 关系本身过了（引句是真的、端点是搭的），连得对不对留给人判断
        assert len(relations) == 1

    def test_自环被拒(self):
        _, _, result = _run(
            [_level_item()],
            [
                {
                    "subject": "Ⅱ级响应",
                    "predicate": "RESPONDS_WITH",
                    "object": "Ⅱ级响应",
                    "chunk": 1,
                    "quote": "启动Ⅱ级响应",
                }
            ],
        )
        assert result.reject_reasons["self_relation"] == 1

    def test_同一事实多处引用只留一条待审(self):
        """同一条事实在两段原文里都写了是同一件事，待审区不该出现两遍。"""
        _, relations, _ = _run(
            [_level_item(), _measure_item()],
            [
                {
                    "subject": "Ⅱ级响应",
                    "predicate": "RESPONDS_WITH",
                    "object": "加强水情监测",
                    "chunk": 1,
                    "quote": "Ⅱ级响应应采取以下措施：加强水情监测",
                },
                {
                    "subject": "Ⅱ级响应",
                    "predicate": "RESPONDS_WITH",
                    "object": "加强水情监测",
                    "chunk": 1,
                    "quote": "加强水情监测",
                },
            ],
        )
        assert len(relations) == 1

    def test_名字上的引号书名号不影响连线(self):
        _, relations, _ = _run(
            [_level_item(name="Ⅱ级响应"), _measure_item()],
            [
                {
                    "subject": "「Ⅱ级响应」",
                    "predicate": "RESPONDS_WITH",
                    "object": "《加强水情监测》",
                    "chunk": 1,
                    "quote": "Ⅱ级响应应采取以下措施：加强水情监测",
                }
            ],
        )
        assert len(relations) == 1


class TestKnownCatalog:
    def test_中文名和原名都能查到(self):
        """模型和人都可能用实例的原始名；只按 display_zh 建索引会让原名查不到。"""
        by_name, by_uid, ambiguous = proposals.known_from_instances(
            [
                SimpleNamespace(
                    uid="x:1", entity_type="measure", name="m1", props={"display_zh": "加强巡查"}
                )
            ]
        )
        assert by_name["加强巡查"].uid == "x:1"
        assert by_name["m1"].uid == "x:1"
        assert by_uid["x:1"].name == "加强巡查"
        assert ambiguous == set()

    def test_同名不同实例整条作废而不是随便挑一个(self):
        """连错边比连不上更坏——同名时谁都不认，交给人。"""
        by_name, _, ambiguous = proposals.known_from_instances(
            [
                SimpleNamespace(uid="x:1", entity_type="measure", name="m1", props={}),
                SimpleNamespace(uid="x:2", entity_type="measure", name="m2", props={}),
            ]
        )
        # 两个实例的原始名不同，各自都能查到
        assert by_name["m1"].uid == "x:1" and by_name["m2"].uid == "x:2"
        assert ambiguous == set()

        by_name, _, ambiguous = proposals.known_from_instances(
            [
                SimpleNamespace(uid="x:1", entity_type="measure", name="同名", props={}),
                SimpleNamespace(uid="x:2", entity_type="measure", name="同名", props={}),
            ]
        )
        assert "同名" not in by_name
        assert ambiguous == {"同名"}
