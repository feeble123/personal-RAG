"""单元 C 方向1：focus_rerank_query 聚焦词提取回归测试。

背景：检索 rerank 前会先把问题浓缩成「核心主题词」，但旧逻辑把「定义/依据/构建」等
关系词误当有效词（「水力半径的定义」只剩「定义」→ 搜不到「水力半径」），且「是多少/
分为/有几级」等问法未识别（整句原样拿去 rerank 被噪声稀释）。

本测试锁定：关系词跳过 + 新问法识别 + 正常问句不退化。
"""
from __future__ import annotations

from app.services.rag import focus_rerank_query


class TestRelationWordSkip:
    """「X的{关系词}」→ 取 X（跳过定义/依据/构建/数量/等级等关系词）。"""

    def test_definition_skipped(self):
        assert focus_rerank_query("水力半径的定义是什么？") == "水力半径"

    def test_basis_skipped(self):
        # 「有压管道按长管计算」过长触发守卫回退原句，但绝不能只剩「依据」
        out = focus_rerank_query("有压管道按长管计算的依据是什么？")
        assert out != "依据"

    def test_construction_skipped(self):
        assert focus_rerank_query("洪水分析模型的构建包括哪些步骤？") == "洪水分析模型"

    def test_quantity_skipped(self):
        assert focus_rerank_query("动力配电箱的数量是多少？") == "动力配电箱"

    def test_duty_skipped(self):
        # 「X及职责」的「职责」是关系词，不能单独当聚焦词——
        # 否则「应急组织机构及职责」只剩「职责」，rerank 被满篇「施工组织机构」块挤掉。
        out = focus_rerank_query("高支模应急组织机构及职责是什么？")
        assert out != "职责"
        assert "应急组织机构" in out


class TestNewQuestionForms:
    """此前未识别的问法现在能正确提取主题词。"""

    def test_fenwei_level(self):
        assert focus_rerank_query("预警信息分为哪几个等级？") == "预警信息"

    def test_fenwei_ji_duo_level(self):
        assert focus_rerank_query("应急预案中的应急响应分为几级？") == "应急响应"

    def test_fenwei_jilei(self):
        assert focus_rerank_query("水力学中水头损失分为哪几类？") == "水头损失"


class TestNoRegression:
    """正常问句的聚焦词不因改动而退化（对照组）。"""

    def test_compound_condition_preserved(self):
        # 「形成条件」是有实义的复合词，不能被当成关系词拆掉
        assert focus_rerank_query("明渠均匀流的形成条件是什么？") == "形成条件"

    def test_compound_formula_preserved(self):
        # 「断面面积公式」复合词保留（不能只取「公式」）
        assert focus_rerank_query("明渠流动的断面面积公式是什么？") == "断面面积公式"

    def test_technical_requirement_preserved(self):
        # 「技术要求」复合词保留
        assert focus_rerank_query("脚手架搭设的技术要求有哪些？") == "技术要求"

    def test_water_depth_calc_preserved(self):
        # 「如何计算」类取「水深」，不受「计算」关系词误伤（计算在问法侧，非主题侧）
        assert focus_rerank_query("明渠均匀流的水深如何计算？") == "水深"

    def test_unknown_form_falls_back_to_original(self):
        # 无匹配问法时回退原句，不硬猜（防误伤）
        q = "随便一句没有问法的话"
        assert focus_rerank_query(q) == q
