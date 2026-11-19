"""把高洞子水库四份专题报告的防汛处置知识灌成本体实例与关系。

素材（同一个知识库）：
    调度规程 / 汛期调度运用计划 / 防洪抢险应急预案 / 大坝安全管理应急预案

**每条实例和每个关系都必须带原文出处。** 出处写成「文件 + 原文引句」，
由本脚本回数据库里找到对应切片（chunk_id），找不到就报错退出——
宁可少建一条，也不要建一条无法回溯到原文的"事实"。

用法（backend 目录下）：

    python -m scripts.seed_reservoir_flood --dry-run   # 只解析出处，不写库
    python -m scripts.seed_reservoir_flood             # 写库 + 同步图库

幂等：实例按 (profile, 类型, 名称, kb) 更新，关系按三元组更新；重复跑不会重复建。
"""
from __future__ import annotations

import argparse
import asyncio
import re
import sys
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from sqlalchemy import select  # noqa: E402

from app.db.models import Chunk, Document  # noqa: E402
from app.db.session import async_session_factory  # noqa: E402
from app.services.graph.ontology import instances as inst_mod  # noqa: E402
from app.services.graph.ontology import relations as rel_mod  # noqa: E402
from app.services.graph.ontology.registry import get_registry  # noqa: E402
from app.services.graph.store import get_graph_store  # noqa: E402

PROFILE = "reservoir_flood"
KB_NAME = "高洞子水库防洪专题报告"

S = "高洞子水库调度规程报告.pdf"        # 调度规程
P = "高洞子水库汛期调度运用计划.pdf"     # 汛期调度运用计划
F = "高洞子水库防洪预案.pdf"             # 防洪抢险应急预案
D = "高洞子水库大坝安全管理应急预案.pdf"  # 大坝安全管理应急预案


def norm(text: str) -> str:
    """比对用的归一化：去空白、统一上下标与全角括号。

    MinerU 会把数字拆开（`1 3 0 m^3 / s`），所以必须去空白再比；
    上标 ³ 与 ^3 写法不一，也一并归一。
    """
    out = (text or "").replace("³", "3").replace("^", "").replace("&nbsp;", "")
    out = out.replace("（", "(").replace("）", ")").replace("，", ",").replace("：", ":")
    out = re.sub(r"[\s　]+", "", out)
    return out


# ============================================================
# 实例：(类型, 名称, 属性, [(文件, 原文引句), ...])
# ============================================================
INSTANCES: list[tuple[str, str, dict[str, Any], list[tuple[str, str]]]] = [
    # ---------- 工程对象 ----------
    ("engineering_object", "高洞子水库",
     {"category": "水库", "grade": "Ⅲ等中型", "total_storage_wan_m3": 1025,
      "max_dam_height_m": 42.6, "dam_type": "沥青混凝土心墙土石坝"},
     [(P, "高洞子水库总库容为1025万m³，最大坝高42.60m")]),
    ("engineering_object", "溢洪道", {"category": "溢洪道"},
     [(F, "立即开启生态放水管阀门及溢洪道闸门")]),
    ("engineering_object", "生态放水管", {"category": "放水管"},
     [(F, "立即开启生态放水管阀门及溢洪道闸门")]),
    ("engineering_object", "溢洪道闸门", {"category": "闸门"},
     [(S, "表3-1 四种下泄量下对应的水位-闸门开度表")]),

    # ---------- 特征水位 ----------
    ("water_level", "校核洪水位",
     {"kind": "校核洪水位", "elevation_m": 343.54, "storage_wan_m3": 1025},
     [(P, "1 | 校核洪水 | 343.54 | 1025")]),
    ("water_level", "设计洪水位",
     {"kind": "设计洪水位", "elevation_m": 343, "storage_wan_m3": 994},
     [(P, "2 | 设计洪水 | 343 | 994")]),
    ("water_level", "正常蓄水位",
     {"kind": "正常蓄水位", "elevation_m": 343, "storage_wan_m3": 994},
     [(P, "3 | 正常蓄水位 | 343 | 994")]),
    ("water_level", "死水位",
     {"kind": "死水位", "elevation_m": 326, "storage_wan_m3": 226},
     [(P, "4 | 死水位 | 326 | 226")]),
    ("water_level", "汛期初期试运行水位（正常年）",
     {"kind": "汛期试运行水位", "elevation_m": 338.5, "note": "正常年，汛期 5-9 月"},
     [(S, "定出338.5m作为汛期时水库初期试运行水位")]),
    ("water_level", "汛期初期试运行水位（干旱年）",
     {"kind": "汛期试运行水位", "elevation_m": 340.0, "note": "前一年干旱或现年春旱"},
     [(S, "水库初期干旱年的试运行水位为：340m")]),

    # ---------- 工况：按 24h 降雨分档 ----------
    ("condition", "24h降雨小于85mm（1年一遇）",
     {"display_zh": "1年一遇", "return_period": 1, "frequency": 1.0, "rainfall_24h_mm": "<85", "season": "汛期"},
     [(S, "预计24h降雨量小于85mm（1年一遇）")]),
    ("condition", "1~2年一遇降雨（85~100mm）",
     {"display_zh": "1~2年一遇", "return_period": 2, "frequency": 0.5, "rainfall_24h_mm": "85~100", "season": "汛期"},
     [(S, "预计 24h降雨量大于85mm且小于100mm（1~2年一遇）")]),
    ("condition", "2~3年一遇降雨（100~110mm）",
     {"display_zh": "2~3年一遇", "return_period": 3, "frequency": 0.333, "rainfall_24h_mm": "100~110", "season": "汛期"},
     [(S, "预计24h降雨量大于100mm且小于110mm（2~3年一遇）")]),
    ("condition", "3~5年一遇降雨（110~130mm）",
     {"display_zh": "3~5年一遇", "return_period": 5, "frequency": 0.2, "rainfall_24h_mm": "110~130", "season": "汛期"},
     [(S, "大于 110mm且小于130mm（3~5年一遇）")]),
    ("condition", "5~10年一遇降雨（130~155mm）",
     {"display_zh": "5~10年一遇", "return_period": 10, "frequency": 0.1, "rainfall_24h_mm": "130~155", "season": "汛期"},
     [(S, "大于130mm且小于155mm（5~10年一遇）")]),
    ("condition", "10~20年一遇降雨（155~170mm）",
     {"display_zh": "10~20年一遇", "return_period": 20, "frequency": 0.05, "rainfall_24h_mm": "155~170", "season": "汛期"},
     [(S, "24h大于155mm且小于170mm(10~20年一遇)")]),
    ("condition", "20~50年一遇降雨（170~210mm）",
     {"display_zh": "20~50年一遇", "return_period": 50, "frequency": 0.02, "rainfall_24h_mm": "170~210", "season": "汛期"},
     [(S, "24h大于170mm且小于210mm(20~50年一遇)")]),

    # ---------- 工况：按重现期 / 水位判别 ----------
    ("condition", "20年一遇洪水",
     {"return_period": 20, "frequency": 0.05,
      "note": "下游防护对象的防洪标准；也是 Ⅳ 级响应的启动条件之一"},
     [(F, "根据洪水预报，可能遭遇20年一遇的洪水")]),
    ("condition", "20~50年一遇洪水",
     {"return_period": 50, "frequency": 0.02},
     [(F, "可能遭遇20年以上50年以下一遇的洪水")]),
    ("condition", "50~1000年一遇洪水",
     {"return_period": 1000, "frequency": 0.001},
     [(F, "可能遭遇50年以上1000年以下一遇的洪水")]),
    ("condition", "1000年以上一遇洪水",
     {"return_period": 1000, "frequency": 0.001},
     [(F, "可能遭遇1000年以上一遇的洪水")]),
    ("condition", "超下游标准洪水",
     {"return_period": 20, "frequency": 0.05,
      "note": "超过下游防护对象防洪标准（20年一遇），转以 480m³/s 为最大允许下泄量削峰"},
     [(S, "由于下游防护对象的防洪标准为20年一遇")]),
    ("condition", "校核洪水（1000年一遇）",
     {"return_period": 1000, "frequency": 0.001, "inflow_m3s": 1000},
     [(S, "入库流量达到乃至超过校核洪峰流量1000m³/s")]),
    ("condition", "库水位超过正常蓄水位",
     {"level_m": 343.0},
     [(F, "库水位大于正常蓄水位343.00m，小于校核洪水位343.54m时")]),
    ("condition", "库水位超过校核洪水位",
     {"level_m": 343.54},
     [(F, "水库库水位超过校核洪水位343.54m时")]),

    # ---------- 调度方案 ----------
    ("dispatch_plan", "1年一遇控泄方案",
     {"start_level_m": 338.5, "target_level_m": 338.5, "mode": "控泄",
      "priority": "维持试运行水位", "note": "下泄量等于入库量"},
     [(S, "控制溢洪闸开度保持下泄量等于入库量")]),
    ("dispatch_plan", "1~2年一遇控泄方案",
     {"outflow_m3s": 60, "start_level_m": 338.5, "mode": "控泄", "priority": "下游防洪"},
     [(S, "洪水控制下泄流量60m³/s")]),
    ("dispatch_plan", "2~3年一遇控泄方案",
     {"outflow_m3s": 70, "start_level_m": 338.5, "mode": "控泄", "priority": "下游防洪"},
     [(S, "洪水控制下泄流量70m³/s")]),
    ("dispatch_plan", "3~5年一遇控泄方案",
     {"outflow_m3s": 100, "start_level_m": 338.5, "mode": "控泄", "priority": "下游防洪"},
     [(S, "洪水控制下泄流量 1 0 0 m^3 / s")]),
    ("dispatch_plan", "5~10年一遇控泄方案（调度规程）",
     {"outflow_m3s": 120, "start_level_m": 338.5, "mode": "控泄", "priority": "下游防洪",
      "note": "调度规程取 120；同一工况汛期调度运用计划取 125，两份文件不一致"},
     [(S, "洪水控制下泄流量120m³/s")]),
    ("dispatch_plan", "5~10年一遇控泄方案（汛期调度运用计划）",
     {"outflow_m3s": 125, "start_level_m": 338.5, "mode": "控泄", "priority": "下游防洪",
      "note": "运用计划取 125；调度规程取 120，两份文件不一致"},
     [(P, "洪水控制下泄流量125m³/s")]),
    ("dispatch_plan", "10~20年一遇控泄方案",
     {"outflow_m3s": 130, "start_level_m": 338.5, "mode": "控泄", "priority": "下游防洪"},
     [(S, "初期洪水控制下泄流量130m³/s")]),
    ("dispatch_plan", "20~50年一遇控泄方案",
     {"outflow_m3s": 130, "start_level_m": 338.5, "mode": "控泄", "priority": "下游防洪"},
     [(S, "24h大于170mm且小于210mm(20~50年一遇)的情况下，初期洪水控制下泄流量130m³/s")]),
    ("dispatch_plan", "中小洪水控泄方案",
     {"outflow_m3s": 130, "mode": "控泄", "priority": "下游防洪",
      "note": "库水位不超过正常蓄水位 343.00m 的常规控泄"},
     [(S, "大坝控制下泄量不超过 1 3 0 m^3 / s")]),
    ("dispatch_plan", "超标准洪水削峰方案",
     {"outflow_m3s": 450, "mode": "控泄", "priority": "下游防洪",
      "note": "以 20 年一遇洪峰 480m³/s 为最大允许下泄量；来洪第一小时不下泄"},
     [(S, "洪水初期控制下泄流量为 4 5 0 m^3 / s")]),
    ("dispatch_plan", "保坝泄洪方案",
     {"mode": "保坝", "priority": "保大坝安全",
      "note": "开启一切泄洪设施，下泄流量不得大于入库流量"},
     [(S, "开启一切泄洪设施，按下泄流量最大化的原则进行调度，且下泄流量不得大于入库流量")]),

    # ---------- 响应等级 ----------
    ("response_level", "Ⅳ级响应", {"level": "Ⅳ", "color": "蓝色",
                                    "trigger_summary": "6h 雨量 30mm，或可能遭遇 20 年一遇洪水"},
     [(F, "Ⅳ级（蓝色预警）")]),
    ("response_level", "Ⅲ级响应", {"level": "Ⅲ", "color": "黄色",
                                    "trigger_summary": "6h 雨量 50mm，或可能遭遇 20~50 年一遇洪水"},
     [(F, "Ⅲ级（黄色预警）")]),
    ("response_level", "Ⅱ级响应", {"level": "Ⅱ", "color": "橙色",
                                    "trigger_summary": "3h 雨量 50mm，或可能遭遇 50~1000 年一遇洪水"},
     [(F, "4.6.1.3 Ⅱ级险情及应急措施")]),
    ("response_level", "Ⅰ级响应", {"level": "Ⅰ", "color": "红色",
                                    "trigger_summary": "3h 雨量 100mm，或可能遭遇 1000 年以上一遇洪水"},
     [(F, "4.6.1.4 Ⅰ 级险情及应急措施")]),

    # ---------- 处置措施 ----------
    ("measure", "传达应急响应命令",
     {"timing": "Ⅳ/Ⅲ级 1h 内；Ⅱ级 30min 内", "owner_role": "水库管理处"},
     [(F, "高洞子水库管理处人员应在1h内将应急响应命令和有关信息传送到")]),
    ("measure", "向区防指报备",
     {"timing": "与传达响应命令同步", "owner_role": "水库管理处"},
     [(F, "同时向巴南区防汛抗旱应急指挥部报备")]),
    ("measure", "24小时轮流值班", {"owner_role": "水库管理处"},
     [(F, "进行24h轮流值班")]),
    ("measure", "汇报水情工情险情",
     {"frequency": "Ⅳ/Ⅲ级 每 2h；Ⅱ级 每 1h；Ⅰ级 每 0.5h", "owner_role": "水库管理处"},
     [(F, "每2h向高洞子水库防汛抗旱应急领导小组汇报水库水情、工情及险情")]),
    ("measure", "通知下游镇街",
     {"owner_role": "防汛抗旱应急领导小组"},
     [(F, "向圣灯山镇、安澜镇、一品街道人民政府龙洲湾街道及巴南区防汛抗旱应急指挥部通报水库的运行现状")]),
    ("measure", "专家组到场",
     {"timing": "Ⅲ级 2h 内；Ⅱ级 30min 内", "owner_role": "应急抢险专家组"},
     [(F, "专家组成员在接到信息后2h内赴现场进行技术指导")]),
    ("measure", "加强安全巡查", {"owner_role": "水库管理处"},
     [(F, "加大安全巡查力度")]),
    ("measure", "开闸泄水前通知下游村镇",
     {"timing": "泄洪前 1h", "owner_role": "水库管理处"},
     [(S, "需提前1小时通知下游影响范围内的各个村镇")]),
    ("measure", "开闸泄洪",
     {"owner_role": "水库管理处", "note": "按水位-闸门开度表调整开度"},
     [(S, "控制溢洪闸开度保持下泄量等于入库量")]),
    ("measure", "闸门失灵应急处置", {"owner_role": "水库管理处"},
     [(F, "当泄洪闸门失灵，应及时进行检修，必要时可将闸门紧急拆除以保证水库下泄能力")]),
    ("measure", "超标准洪水联合泄洪", {"owner_role": "水库管理处"},
     [(F, "立即开启生态放水管阀门及溢洪道闸门，联合泄洪加大最大下泄流量")]),
    ("measure", "启动防洪抢险应急预案", {"owner_role": "防汛抗旱应急领导小组"},
     [(F, "水位继续上涨，应立即启动防洪抢险应急预案")]),
    ("measure", "险情书面报告", {"timing": "90min 内", "owner_role": "水库管理处"},
     [(F, "并在90分钟内由高洞子水库管理处出具详细书面报告")]),
    ("measure", "发布预警信息", {"owner_role": "区防汛抗旱指挥部"},
     [(F, "拉响预警警报，指派宣传车对水库下游用广播进行预警宣传")]),
    ("measure", "组织人员转移", {"owner_role": "镇街人民政府"},
     [(F, "投亲靠友、借住公房、调运帐篷、搭建临时房屋")]),
    ("measure", "解除预警", {"owner_role": "区防汛抗旱指挥部"},
     [(F, "预警宣布结束")]),

    # ---------- 责任单位 ----------
    ("management_unit", "高洞子水库管理处", {"role": "现场运行管理"},
     [(F, "高洞子水库管理处人员应在1h内")]),
    ("management_unit", "高洞子水库防汛抗旱应急领导小组", {"role": "应急指挥（现场）"},
     [(F, "高洞子水库防汛抗旱应急领导小组")]),
    ("management_unit", "巴南区防汛抗旱指挥部", {"role": "统一调度与应急指挥"},
     [(F, "巴南区防汛抗旱应急指挥部")]),
    ("management_unit", "巴南区水利局", {"role": "分级调度审批"},
     [(P, "由巴南区水利局下达调度指令")]),
    ("management_unit", "重庆市城南水利发展有限公司", {"role": "正常蓄水位以下调度权"},
     [(P, "水库调度权限归重庆市城南水利发展有限公司")]),
    ("management_unit", "重庆市水利投资（集团）有限公司", {"role": "上级主管单位"},
     [(D, "重庆市水利投资（集团）有限公司")]),
    ("management_unit", "应急抢险专家组", {"role": "技术指导"},
     [(F, "应急抢险专家组")]),

    # ---------- 影响区域 ----------
    ("admin_region", "圣灯山镇", {"level": "镇", "flood_standard": "20年一遇"},
     [(S, "下游防护对象主要为圣灯山镇、安澜镇、一品街道、龙洲湾街道、鱼洞街道")]),
    ("admin_region", "安澜镇", {"level": "镇", "flood_standard": "20年一遇"},
     [(S, "下游防护对象主要为圣灯山镇、安澜镇、一品街道、龙洲湾街道、鱼洞街道")]),
    ("admin_region", "一品街道", {"level": "街道", "flood_standard": "20年一遇"},
     [(S, "下游防护对象主要为圣灯山镇、安澜镇、一品街道、龙洲湾街道、鱼洞街道")]),
    ("admin_region", "龙洲湾街道", {"level": "街道", "flood_standard": "20年一遇"},
     [(S, "下游防护对象主要为圣灯山镇、安澜镇、一品街道、龙洲湾街道、鱼洞街道")]),
    ("admin_region", "鱼洞街道", {"level": "街道", "flood_standard": "20年一遇"},
     [(S, "下游防护对象主要为圣灯山镇、安澜镇、一品街道、龙洲湾街道、鱼洞街道")]),

    # ---------- 依据条款 ----------
    ("standard_clause", "调度规程 3.3.1.1", {"doc": S, "clause_no": "3.3.1.1"},
     [(S, "3.3.1.1 正常年的防洪调度")]),
    ("standard_clause", "调度规程 3.3.2.2", {"doc": S, "clause_no": "3.3.2.2"},
     [(S, "3.3.2.2 保枢纽安全的防洪调度")]),
    ("standard_clause", "调度规程 3.1.2", {"doc": S, "clause_no": "3.1.2"},
     [(S, "防洪调度由巴南区防汛抗旱指挥部统一调度，区水利局按调度权限实行分级调度")]),
    ("standard_clause", "运用计划 7.2", {"doc": P, "clause_no": "7.2"},
     [(P, "7.2 防洪调度权限")]),
    ("standard_clause", "运用计划 7.3.1.1", {"doc": P, "clause_no": "7.3.1.1"},
     [(P, "7.3.1.1 正常年的防洪调度")]),
    ("standard_clause", "防洪预案 4.2.3", {"doc": F, "clause_no": "4.2.3"},
     [(F, "4.2.3 调度权限及执行部门")]),
    ("standard_clause", "防洪预案 4.6.1", {"doc": F, "clause_no": "4.6.1"},
     [(F, "4.6.1 预警级别及应急措施")]),
    ("standard_clause", "大坝安全预案 1.5", {"doc": D, "clause_no": "1.5"},
     [(D, "1.5 突发事件分级")]),
    ("standard_clause", "大坝安全预案 4.2", {"doc": D, "clause_no": "4.2"},
     [(D, "4.2 应急响应")]),
]


# ============================================================
# 关系：(主语类型, 主语名, 关系, 宾语类型, 宾语名, [(文件, 引句)])
# ============================================================
RELATIONS: list[tuple[str, str, str, str, str, list[tuple[str, str]]]] = [
    # 工况 → 调度方案（按降雨分档）
    ("condition", "24h降雨小于85mm（1年一遇）", "STIPULATES", "dispatch_plan", "1年一遇控泄方案",
     [(S, "预计24h降雨量小于85mm（1年一遇）")]),
    ("condition", "1~2年一遇降雨（85~100mm）", "STIPULATES", "dispatch_plan", "1~2年一遇控泄方案",
     [(S, "洪水控制下泄流量60m³/s")]),
    ("condition", "2~3年一遇降雨（100~110mm）", "STIPULATES", "dispatch_plan", "2~3年一遇控泄方案",
     [(S, "洪水控制下泄流量70m³/s")]),
    ("condition", "3~5年一遇降雨（110~130mm）", "STIPULATES", "dispatch_plan", "3~5年一遇控泄方案",
     [(S, "洪水控制下泄流量 1 0 0 m^3 / s")]),
    ("condition", "5~10年一遇降雨（130~155mm）", "STIPULATES", "dispatch_plan", "5~10年一遇控泄方案（调度规程）",
     [(S, "洪水控制下泄流量120m³/s")]),
    ("condition", "5~10年一遇降雨（130~155mm）", "STIPULATES", "dispatch_plan", "5~10年一遇控泄方案（汛期调度运用计划）",
     [(P, "洪水控制下泄流量125m³/s")]),
    ("condition", "10~20年一遇降雨（155~170mm）", "STIPULATES", "dispatch_plan", "10~20年一遇控泄方案",
     [(S, "24h大于155mm且小于170mm(10~20年一遇)")]),
    ("condition", "20~50年一遇降雨（170~210mm）", "STIPULATES", "dispatch_plan", "20~50年一遇控泄方案",
     [(S, "24h大于170mm且小于210mm(20~50年一遇)")]),

    # 20 年一遇：落在 10~20 年一遇这一档，同时是 Ⅳ 级响应的启动条件
    ("condition", "20年一遇洪水", "INSTANCE_OF", "condition", "10~20年一遇降雨（155~170mm）",
     [(S, "24h大于155mm且小于170mm(10~20年一遇)")]),
    ("condition", "20年一遇洪水", "TRIGGERS", "response_level", "Ⅳ级响应",
     [(F, "根据洪水预报，可能遭遇20年一遇的洪水")]),
    ("condition", "20~50年一遇洪水", "TRIGGERS", "response_level", "Ⅲ级响应",
     [(F, "可能遭遇20年以上50年以下一遇的洪水")]),
    ("condition", "50~1000年一遇洪水", "TRIGGERS", "response_level", "Ⅱ级响应",
     [(F, "可能遭遇50年以上1000年以下一遇的洪水")]),
    ("condition", "1000年以上一遇洪水", "TRIGGERS", "response_level", "Ⅰ级响应",
     [(F, "可能遭遇1000年以上一遇的洪水")]),

    # 超标准 / 保坝
    ("condition", "超下游标准洪水", "STIPULATES", "dispatch_plan", "超标准洪水削峰方案",
     [(S, "由于下游防护对象的防洪标准为20年一遇")]),
    ("condition", "校核洪水（1000年一遇）", "STIPULATES", "dispatch_plan", "保坝泄洪方案",
     [(S, "入库流量达到乃至超过校核洪峰流量1000m³/s时，水位仍继续上涨，为保大坝安全")]),

    # 调度权限（水位判别）
    ("condition", "库水位超过正常蓄水位", "PERFORMED_BY", "management_unit", "巴南区水利局",
     [(P, "由巴南区水利局下达调度指令")]),
    ("condition", "库水位超过校核洪水位", "PERFORMED_BY", "management_unit", "巴南区防汛抗旱指挥部",
     [(P, "由巴南区防汛抗旱指挥部按照应急预案的启动方式启动应急预案")]),

    # 响应等级 → 处置措施
    ("response_level", "Ⅳ级响应", "RESPONDS_WITH", "measure", "传达应急响应命令",
     [(F, "高洞子水库管理处人员应在1h内将应急响应命令和有关信息传送到")]),
    ("response_level", "Ⅳ级响应", "RESPONDS_WITH", "measure", "向区防指报备",
     [(F, "同时向巴南区防汛抗旱应急指挥部报备")]),
    ("response_level", "Ⅳ级响应", "RESPONDS_WITH", "measure", "24小时轮流值班",
     [(F, "进行24h轮流值班")]),
    ("response_level", "Ⅳ级响应", "RESPONDS_WITH", "measure", "汇报水情工情险情",
     [(F, "每2h向高洞子水库防汛抗旱应急领导小组汇报水库水情、工情及险情")]),
    ("response_level", "Ⅳ级响应", "RESPONDS_WITH", "measure", "通知下游镇街",
     [(F, "向圣灯山镇、安澜镇、一品街道人民政府龙洲湾街道及巴南区防汛抗旱应急指挥部通报水库的运行现状")]),
    ("response_level", "Ⅳ级响应", "RESPONDS_WITH", "measure", "加强安全巡查",
     [(F, "加大安全巡查力度")]),
    ("response_level", "Ⅲ级响应", "RESPONDS_WITH", "measure", "专家组到场",
     [(F, "专家组成员在接到信息后2h内赴现场进行技术指导")]),
    ("response_level", "Ⅲ级响应", "RESPONDS_WITH", "measure", "传达应急响应命令",
     [(F, "高洞子水库管理处人员应在1h内将应急响应命令和有关信息传送到")]),
    ("response_level", "Ⅲ级响应", "RESPONDS_WITH", "measure", "汇报水情工情险情",
     [(F, "每2h向高洞子水库防汛抗旱应急领导小组汇报水库水情、工情及险情")]),
    ("response_level", "Ⅱ级响应", "RESPONDS_WITH", "measure", "专家组到场",
     [(F, "专家组成员在接到信息后30min内赴现场进行技术指导")]),
    ("response_level", "Ⅱ级响应", "RESPONDS_WITH", "measure", "传达应急响应命令",
     [(F, "高洞子水库管理处人员应在30min内将应急响应命令和有关信息传送到")]),
    ("response_level", "Ⅱ级响应", "RESPONDS_WITH", "measure", "汇报水情工情险情",
     [(F, "每1h向高洞子水库防汛抗旱应急领导小组汇报水库水情、工情及险情")]),
    ("response_level", "Ⅰ级响应", "RESPONDS_WITH", "measure", "汇报水情工情险情",
     [(F, "每0.5h向巴南区防汛抗旱应急指挥部办公室汇报水库水情、工情及险情")]),
    ("response_level", "Ⅰ级响应", "RESPONDS_WITH", "measure", "启动防洪抢险应急预案",
     [(F, "水库库水位超过校核洪水位343.54m时，由高洞子水库管理处向上级上报水库当前险情")]),

    # 调度方案 → 必须完成的动作
    ("dispatch_plan", "中小洪水控泄方案", "REQUIRES", "measure", "开闸泄水前通知下游村镇",
     [(S, "高洞子水库需要开闸泄水时，需提前1小时通知下游影响范围内的各个村镇")]),
    ("dispatch_plan", "中小洪水控泄方案", "REQUIRES", "measure", "开闸泄洪",
     [(S, "遇中小洪水（水位不超过正常蓄水位343.00m）")]),
    ("dispatch_plan", "保坝泄洪方案", "REQUIRES", "measure", "超标准洪水联合泄洪",
     [(F, "立即开启生态放水管阀门及溢洪道闸门，联合泄洪加大最大下泄流量")]),

    # 处置流程的先后
    ("measure", "开闸泄水前通知下游村镇", "NEXT_STEP", "measure", "开闸泄洪",
     [(S, "需提前1小时通知下游影响范围内的各个村镇")]),
    ("measure", "超标准洪水联合泄洪", "NEXT_STEP", "measure", "启动防洪抢险应急预案",
     [(F, "当以上措施仍不能安全泄洪，水位继续上涨，应立即启动防洪抢险应急预案")]),
    ("measure", "闸门失灵应急处置", "NEXT_STEP", "measure", "开闸泄洪",
     [(F, "当泄洪闸门失灵，应及时进行检修，必要时可将闸门紧急拆除以保证水库下泄能力")]),

    # 谁执行
    ("measure", "传达应急响应命令", "PERFORMED_BY", "management_unit", "高洞子水库管理处",
     [(F, "高洞子水库管理处人员应在1h内将应急响应命令")]),
    ("measure", "向区防指报备", "PERFORMED_BY", "management_unit", "高洞子水库管理处",
     [(F, "同时向巴南区防汛抗旱应急指挥部报备")]),
    ("measure", "24小时轮流值班", "PERFORMED_BY", "management_unit", "高洞子水库管理处",
     [(F, "高洞子水库管理处进行24h轮流值班")]),
    ("measure", "汇报水情工情险情", "PERFORMED_BY", "management_unit", "高洞子水库管理处",
     [(F, "高洞子水库管理处密切关注水位，雨情，加大安全巡查力度")]),
    ("measure", "通知下游镇街", "PERFORMED_BY", "management_unit", "高洞子水库防汛抗旱应急领导小组",
     [(F, "高洞子水库防汛抗旱应急领导小组向圣灯山镇")]),
    ("measure", "专家组到场", "PERFORMED_BY", "management_unit", "应急抢险专家组",
     [(F, "专家组成员在接到信息后2h内赴现场进行技术指导")]),
    ("measure", "险情书面报告", "PERFORMED_BY", "management_unit", "高洞子水库管理处",
     [(F, "并在90分钟内由高洞子水库管理处出具详细书面报告")]),

    # 影响谁
    ("measure", "通知下游镇街", "AFFECTS", "admin_region", "圣灯山镇",
     [(F, "向圣灯山镇、安澜镇、一品街道人民政府龙洲湾街道")]),
    ("measure", "通知下游镇街", "AFFECTS", "admin_region", "安澜镇",
     [(F, "向圣灯山镇、安澜镇、一品街道人民政府龙洲湾街道")]),
    ("measure", "通知下游镇街", "AFFECTS", "admin_region", "一品街道",
     [(F, "向圣灯山镇、安澜镇、一品街道人民政府龙洲湾街道")]),
    ("measure", "通知下游镇街", "AFFECTS", "admin_region", "龙洲湾街道",
     [(F, "向圣灯山镇、安澜镇、一品街道人民政府龙洲湾街道")]),
    ("measure", "开闸泄水前通知下游村镇", "AFFECTS", "admin_region", "圣灯山镇",
     [(S, "需提前1小时通知下游影响范围内的各个村镇")]),
    ("measure", "组织人员转移", "PROTECTS", "admin_region", "圣灯山镇",
     [(F, "应急转移人员安置方式为：投亲靠友、借住公房、调运帐篷、搭建临时房屋")]),

    # 用什么设施
    ("measure", "开闸泄洪", "USES_FACILITY", "engineering_object", "溢洪道闸门",
     [(S, "控制溢洪闸开度保持下泄量等于入库量")]),
    ("measure", "超标准洪水联合泄洪", "USES_FACILITY", "engineering_object", "生态放水管",
     [(F, "立即开启生态放水管阀门及溢洪道闸门")]),
    ("measure", "超标准洪水联合泄洪", "USES_FACILITY", "engineering_object", "溢洪道闸门",
     [(F, "立即开启生态放水管阀门及溢洪道闸门")]),

    # 依据哪条
    ("dispatch_plan", "1~2年一遇控泄方案", "CONSTRAINED_BY", "standard_clause", "调度规程 3.3.1.1",
     [(S, "3.3.1.1 正常年的防洪调度")]),
    ("dispatch_plan", "5~10年一遇控泄方案（汛期调度运用计划）", "CONSTRAINED_BY",
     "standard_clause", "运用计划 7.3.1.1", [(P, "7.3.1.1 正常年的防洪调度")]),
    ("dispatch_plan", "保坝泄洪方案", "CONSTRAINED_BY", "standard_clause", "调度规程 3.3.2.2",
     [(S, "3.3.2.2 保枢纽安全的防洪调度")]),
    ("measure", "开闸泄水前通知下游村镇", "CONSTRAINED_BY", "standard_clause", "调度规程 3.3.1.1",
     [(S, "需提前1小时通知下游影响范围内的各个村镇")]),
    ("measure", "传达应急响应命令", "CONSTRAINED_BY", "standard_clause", "防洪预案 4.6.1",
     [(F, "4.6.1 预警级别及应急措施")]),
    ("measure", "启动防洪抢险应急预案", "CONSTRAINED_BY", "standard_clause", "防洪预案 4.2.3",
     [(F, "4.2.3 调度权限及执行部门")]),

    # 单位之间的报告/指挥关系
    ("management_unit", "高洞子水库管理处", "REPORTS_TO", "management_unit", "高洞子水库防汛抗旱应急领导小组",
     [(F, "上报高洞子水库防汛应急领导小组")]),
    ("management_unit", "高洞子水库防汛抗旱应急领导小组", "REPORTS_TO", "management_unit", "巴南区防汛抗旱指挥部",
     [(F, "再由高洞子水库防汛应急领导小组上报至区防办")]),
    ("management_unit", "巴南区防汛抗旱指挥部", "REPORTS_TO", "management_unit", "重庆市水利投资（集团）有限公司",
     [(F, "同时向巴南区防汛抗旱指挥部和重庆市水利投资（集团）有限公司报告")]),
]


async def _load_chunks(db) -> dict[str, list[tuple[int, str, int | None]]]:
    """文档名 → [(chunk_id, 归一化正文, 印刷页码)]"""
    rows = (
        await db.execute(
            select(Document.id, Document.filename, Chunk.id, Chunk.content, Chunk.printed_page)
            .join(Chunk, Chunk.document_version_id == Document.active_version_id)
        )
    ).all()
    out: dict[str, list[tuple[int, str, int | None]]] = {}
    for _doc_id, filename, chunk_id, content, printed in rows:
        out.setdefault(filename, []).append((int(chunk_id), norm(content), printed))
    return out


def _resolve(
    index: dict[str, list[tuple[int, str, int | None]]], doc: str, quote: str
) -> dict[str, Any] | None:
    """按文件 + 引句找切片；找不到返回 None（调用方记为错误）。"""
    needle = norm(quote)
    if not needle:
        return None
    for chunk_id, body, printed in index.get(doc, []):
        if needle in body:
            return {
                "type": "原文",
                "doc_name": doc,
                "chunk_id": chunk_id,
                "printed_page": printed,
                "quote": quote,
                "note": quote,
            }
    return None


async def main() -> int:
    ap = argparse.ArgumentParser(description="导入高洞子水库防汛处置本体实例")
    ap.add_argument("--dry-run", action="store_true", help="只解析出处，不写库")
    ap.add_argument("--kb", default=KB_NAME, help="知识库名称")
    args = ap.parse_args()

    reg = get_registry(PROFILE)
    async with async_session_factory() as db:
        kb_id = (
            await db.execute(select(Document.kb_id).where(Document.filename == S).limit(1))
        ).scalar()
        if kb_id is None:
            print(f"找不到文档 {S}，先把四份报告入库", file=sys.stderr)
            return 1
        index = await _load_chunks(db)

        drafts: list[inst_mod.InstanceDraft] = []
        errors: list[str] = []
        used_evidence = 0
        for etype, name, props, evs in INSTANCES:
            evidence = []
            for doc, quote in evs:
                hit = _resolve(index, doc, quote)
                if hit is None:
                    errors.append(f"实例「{name}」出处找不到：{doc} ← {quote[:40]}")
                else:
                    evidence.append(hit)
            used_evidence += len(evidence)
            drafts.append(
                inst_mod.InstanceDraft(
                    profile_id=PROFILE,
                    entity_type=etype,
                    name=name,
                    uid=reg.uid_for(etype, name, kb_id=kb_id),
                    kb_id=kb_id,
                    props=props,
                    evidence=evidence,
                    source="manual",
                )
            )

        uids = {(d.entity_type, d.name): d.uid for d in drafts}
        rel_drafts: list[rel_mod.RelationDraft] = []
        seen_rel: set[tuple] = set()
        for st, sn, pred, ot, on, evs in RELATIONS:
            su = uids.get((st, sn))
            ou = uids.get((ot, on))
            if su is None or ou is None:
                errors.append(f"关系端点不存在：{sn} --{pred}--> {on}")
                continue
            key = (su, pred, ou)
            if key in seen_rel:
                continue
            seen_rel.add(key)
            evidence = []
            for doc, quote in evs:
                hit = _resolve(index, doc, quote)
                if hit is None:
                    errors.append(f"关系「{sn}-{pred}->{on}」出处找不到：{doc} ← {quote[:40]}")
                else:
                    evidence.append(hit)
            used_evidence += len(evidence)
            rel_drafts.append(
                rel_mod.RelationDraft(
                    profile_id=PROFILE,
                    subject_uid=su,
                    predicate=pred,
                    object_uid=ou,
                    kb_id=kb_id,
                    evidence=evidence,
                    source="manual",
                )
            )

        print(
            f"知识库 kb_id={kb_id}｜实例 {len(drafts)} 个、关系 {len(rel_drafts)} 条、"
            f"证据条目 {used_evidence} 条"
        )
        if errors:
            print(f"\n有 {len(errors)} 处出处对不上（未写入）：", file=sys.stderr)
            for e in errors[:40]:
                print("  -", e, file=sys.stderr)
            if len(errors) > 40:
                print(f"  …还有 {len(errors) - 40} 条", file=sys.stderr)
            return 1

        if args.dry_run:
            print("--dry-run：出处全部对上，未写库")
            return 0

        result = await inst_mod.upsert_instances(db, drafts)
        rel_count = 0
        for rd in rel_drafts:
            await rel_mod.upsert_relation(db, rd)
            rel_count += 1
        await db.commit()

        rows = (await db.execute(
            select(inst_mod.OntologyInstance).where(inst_mod.OntologyInstance.profile_id == PROFILE)
        )).scalars().all()
        store = get_graph_store()
        nodes_written = await inst_mod.sync_to_graph(rows, store) if store else 0
        rel_rows = (await db.execute(
            select(rel_mod.OntologyRelation).where(rel_mod.OntologyRelation.profile_id == PROFILE)
        )).scalars().all()
        edges_written = await rel_mod.sync_to_graph(rel_rows, store) if store else 0

        print(
            f"实例：新建 {result.created}／更新 {result.updated}／不变 {result.unchanged}；"
            f"关系写入 {rel_count} 条"
        )
        print(f"图库同步：节点 {nodes_written}/{len(rows)}，边 {edges_written}/{len(rel_rows)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
