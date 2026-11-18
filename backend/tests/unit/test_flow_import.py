"""O8-1 流程链条导入：一行 = 一个动作，程序推导出四类边。

流程长什么样是人定的——表里没写的就不建，缺类型/关系直接报错，不偷偷造。
"""
from __future__ import annotations

import io

import pytest

from app.services.graph.ontology import flow_import
from app.services.graph.ontology.registry import get_registry


def _xlsx(headers, rows) -> io.BytesIO:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.append(headers)
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


def _reg():
    return get_registry("process_skeleton")


HEADERS = ["阶段", "阶段序号", "动作", "工具", "工具来源", "需要的数据", "数据格式", "说明"]
ROWS = [
    ["数据准备", 1, "下载流域边界", "水文站网API", "外部", "流域边界矢量", "shp", ""],
    [None, None, "整理降雨资料", "站点数据整理", "内部", "逐时降雨", "csv", "缺测值要标出来"],
    ["建模", 2, "配置模型参数", "参数表编辑器", "内部", "参数先验表", "xlsx", ""],
    ["率定", 3, "自动率定", "SCE-UA 率定器", "内部", "率定输入集", "csv", ""],
]


class TestBuildPlan:
    def test_建出四类实例和四类关系(self):
        plan = flow_import.build_plan(_xlsx(HEADERS, ROWS), _reg())
        counts = plan.counts
        assert counts["stages"] == 3
        assert counts["actions"] == 4
        assert counts["tools"] == 4
        assert counts["data_assets"] == 4
        # 阶段链 2 条 + 阶段→动作 4 条 + 动作→工具 4 条 + 动作→数据 4 条
        assert counts["relations"] == 14
        assert not plan.errors

    def test_阶段留空沿用上一行(self):
        plan = flow_import.build_plan(_xlsx(HEADERS, ROWS), _reg())
        assert plan.rows[1].stage == "数据准备"  # 第二行阶段列是空的
        assert plan.stages == {"数据准备": 1, "建模": 2, "率定": 3}

    def test_阶段序号不填按出现顺序(self):
        headers = ["阶段", "动作"]
        rows = [["准备", "a"], ["建模", "b"], ["率定", "c"]]
        plan = flow_import.build_plan(_xlsx(headers, rows), _reg())
        assert plan.stages == {"准备": 1, "建模": 2, "率定": 3}

    def test_同名工具跨阶段只建一次(self):
        headers = ["阶段", "动作", "工具"]
        rows = [["S1", "a1", "共用工具"], ["S2", "a2", "共用工具"]]
        plan = flow_import.build_plan(_xlsx(headers, rows), _reg())
        assert plan.counts["tools"] == 1
        assert sum(1 for r in plan.relations if r.predicate == "USES_TOOL") == 2

    def test_工具来源中文映射成枚举(self):
        headers = ["阶段", "动作", "工具", "工具来源"]
        rows = [["S1", "a1", "t1", "外部"], ["S1", "a2", "t2", "认不出的来源"]]
        plan = flow_import.build_plan(_xlsx(headers, rows), _reg())
        tools = {i.name: i.props for i in plan.instances if i.entity_type == "Tool"}
        assert tools["t1"]["kind"] == "external"
        assert "kind" not in tools["t2"]  # 认不出就不写，不硬塞一个错枚举

    def test_认不出的表头列出来不猜(self):
        headers = HEADERS + ["负责人"]
        rows = [r + ["张三"] for r in ROWS]
        plan = flow_import.build_plan(_xlsx(headers, rows), _reg())
        assert plan.skipped_headers == ["负责人"]

    def test_缺阶段或动作列直接报错(self):
        with pytest.raises(ValueError, match="阶段"):
            flow_import.build_plan(_xlsx(["动作"], [["a"]]), _reg())

    def test_库缺流程类型时说清楚(self):
        tuning = get_registry("tuning")
        with pytest.raises(ValueError, match="缺少流程类型"):
            flow_import.build_plan(_xlsx(HEADERS, ROWS), tuning)

    def test_空表返回空计划(self):
        plan = flow_import.build_plan(_xlsx(HEADERS, []), _reg())
        assert plan.rows == [] and plan.relations == []
