"""链路正文（C 方案的 B 步）：模型只做措辞，编造数值必须被挡住。

为什么校验数值而不是校验全部：模型编造一个新的数字（下泄 3500 m³/s）最容易被当成事实，
危害也最大；名字/地名编造通常一眼能看出来，数值编造看起来最"专业"。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services.graph.ontology import chain_narrative as N

CHAIN = {
    "subject": "青溪水库",
    "conditions": ["20年一遇（P≈5.0%）"],
    "steps": [
        {
            "phase": "basis",
            "phase_label": "工况与依据",
            "hop": 1,
            "subject": "青溪水库",
            "predicate": "STIPULATES",
            "predicate_label": "规定",
            "object": "预泄方案A（提前 24 小时，开 2 孔）",
            "evidence": [
                {"chunk_id": 1, "doc_name": "青溪水库调度规程", "page": 171, "printed_page": 157, "text": ""}
            ],
        },
        {
            "phase": "impact",
            "phase_label": "影响谁",
            "hop": 2,
            "subject": "预泄方案A",
            "predicate": "AFFECTS",
            "predicate_label": "影响",
            "object": "杨柳镇",
            "evidence": [],
        },
    ],
    "gaps": ["预泄方案A —影响→ 杨柳镇"],
}


class _Model:
    def __init__(self, text):
        self.text = text

    async def astream(self, _messages):
        for part in [self.text[:10], self.text[10:]]:
            yield SimpleNamespace(content=part)


def _factory(text):
    return lambda _temp: _Model(text)


class TestPrompt:
    def test_骨架事实全部进提示词(self):
        prompt = N.build_user_prompt("20年一遇怎么调度", CHAIN)
        assert "青溪水库" in prompt and "预泄方案A" in prompt
        assert "青溪水库调度规程 p.157" in prompt  # 优先书内印刷页码
        assert "无出处" in prompt  # 杨柳镇那条没有证据，必须标出来

    def test_有缺口时提示没有出处(self):
        prompt = N.build_user_prompt("q", CHAIN)
        assert "没有任何出处" in prompt

    def test_全部有出处时明确叫模型别加说明(self):
        chain = dict(CHAIN, gaps=[])
        prompt = N.build_user_prompt("q", chain)
        assert "全部有出处" in prompt

    def test_白名单包含骨架里的数值(self):
        allowed = N.allowed_numbers(CHAIN)
        assert {"20", "5.0", "24", "2", "157", "171"} <= allowed


class TestVerify:
    def test_合法正文通过(self):
        N.verify_narrative("青溪水库按 20 年一遇工况预泄，提前 24 小时开 2 孔。", CHAIN)

    def test_编造数值被拒(self):
        with pytest.raises(N.NarrativeRejected) as exc:
            N.verify_narrative("青溪水库下泄 3500 m³/s。", CHAIN)
        assert "3500" in str(exc.value)

    def test_步骤序号不算编造(self):
        N.verify_narrative("第 2 步，杨柳镇需响应。", CHAIN)


class TestWriteNarrative:
    async def test_正常返回正文(self):
        text, reason = await N.write_narrative(
            "20年一遇怎么调度", CHAIN,
            model_factory=_factory("青溪水库按 20 年一遇预泄，提前 24 小时开 2 孔【青溪水库调度规程 p.157】。"),
        )
        assert reason is None
        assert "提前 24 小时" in text

    async def test_编造数值时整段丢弃(self):
        text, reason = await N.write_narrative(
            "20年一遇怎么调度", CHAIN,
            model_factory=_factory("青溪水库下泄 3500 m³/s。"),
        )
        assert text is None
        assert "3500" in reason

    async def test_模型异常时降级(self):
        def boom(_temp):
            raise RuntimeError("中转站 429")

        text, reason = await N.write_narrative("q", CHAIN, model_factory=boom)
        assert text is None
        assert "措辞失败" in reason

    async def test_没有链路就不调用模型(self):
        called = {"n": 0}

        def factory(_temp):
            called["n"] += 1
            return _Model("x")

        text, reason = await N.write_narrative("q", None, model_factory=factory)
        assert (text, called["n"]) == (None, 0)
        assert "没有可叙述" in reason
