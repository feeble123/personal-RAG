"""P1-2 单元B：bake-off 指标计算（纯函数，可单测）。

对同一批扫描 PDF，比较 RapidOCR 路径 vs MinerU 路径，产出可决策指标：
- 完整度（字符量、条款号召回）
- 准确度（乱码率、常用汉字占比、文字层 GT 相似率）
- 结构（标题数、表格完整度）
- 速度/资源（耗时、内存）

加权评分 + 按文档类型路由表。
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher

# 条款号模式（如 8.2.3、3.1.2-1）
_CLAUSE_RE = re.compile(r"(?<!\d)(\d+(?:\.\d+){1,3})(?:-\d+)?(?!\d)")


def _garble_ratio(text: str) -> float:
    """乱码占比：替换字符 � 与私用区字符（复用 pdf.py 逻辑）。"""
    if not text:
        return 0.0
    bad = text.count("�")
    bad += sum(1 for ch in text if 0xE000 <= ord(ch) <= 0xF8FF)
    return bad / len(text)


def _chinese_common_ratio(text: str) -> float:
    """常用汉字占比：真中文正文由高频字主导（复用 pdf.py 高频字集逻辑的简化版）。"""
    total = sum(1 for ch in text if 0x4E00 <= ord(ch) <= 0x9FFF)
    if total == 0:
        return 0.0
    common = sum(1 for ch in text if ch in _COMMON_HAN)
    return common / total


# 高频汉字集（与 pdf.py 一致，用于判定文本质量）
_COMMON_HAN = set(
    "的一是了我不人在他有这上们来到时大地为子中你说生国年着就那和要她出也得里后自以会家可下而过天去能对小多然于心学么之都好看起发当没成只如事把还用第样道想作种开美总从无情己面最女但现前些所同日手又行意动方期它头经长儿回位分爱老因很给名法间斯知世什两次使身者被高已亲其进此话常与活正感见明问力理尔点文几定本公特做外孩相西果走将月十实向声车全信重三机工物气每并别真打太新比才便夫再书部水像眼等体却加电主界门利海受听表德少克代员许先口由死安写性马光白或住难望教命花结乐色更拉东神记处让母父应直字场平报友关放至张认接告入笑内英军候民岁往何度山觉路带万男边风解叫任金快原吃妈变通师立象数四失满战远格士音轻目条呢病始达深完今提求清王化空业思切怎非找片罗钱语元喜曾离飞科言干流欢约各即指合反题必该论交终林请医晚制球决传画保读运及则房早院量苦火布品近坐产答星精视五连司巴奇管类未朋且婚台夜青北队久乎越观落尽形影红爸百令周吧识步希亚术留市半热送兴造谈容极随演收首根整式取照办强石古华另句纪接元伟测速笑组带志呼干友王李张吴刘陈黄杨周徐孙马朱胡郭何高林罗郑梁谢宋唐许韩冯邓曹彭曾肖田董袁潘于蒋蔡余杜叶程苏魏吕丁任沈姚卢姜崔钟谭陆汪范金石廖贾夏韦付方白邹孟熊秦邱江尹薛阎段雷侯龙史陶黎贺顾毛郝龚邵万钱严覃武戴莫孔向汤"
)


def compute_metrics(text: str, ground_truth: str | None = None) -> dict:
    """计算单引擎输出文本的指标。

    Args:
        text: 引擎产出的全部文本（拼接）
        ground_truth: 文字层 GT 文本（可选，用于相似率）
    """
    total_chars = len(re.sub(r"\s", "", text))
    metrics: dict = {
        "total_chars": total_chars,
        "garble_ratio": round(_garble_ratio(text), 4),
        "chinese_common_ratio": round(_chinese_common_ratio(text), 4),
        "clause_refs": sorted(set(_CLAUSE_RE.findall(text))),
        "clause_count": len(set(_CLAUSE_RE.findall(text))),
    }
    if ground_truth:
        gt_chars = re.sub(r"\s", "", ground_truth)
        if gt_chars:
            sim = SequenceMatcher(None, gt_chars, text.replace(" ", ""))
            metrics["gt_similarity"] = round(sim.ratio(), 4)
        else:
            metrics["gt_similarity"] = None
    return metrics


def weighted_score(metrics: dict, *, time_cost_s: float = 0.0) -> float:
    """加权评分 [0, 100]。

    完整度 30% + 准确度 30% + 结构 25% + 速度/资源 15%。
    完整度/准确度/结构缺失时按 0 计，保证 score 有界。
    """
    # 完整度：字符量（0-5000 归一化）+ 条款数（0-20 归一化），各半
    chars_score = min(1.0, (metrics.get("total_chars") or 0) / 5000)
    clause_score = min(1.0, (metrics.get("clause_count") or 0) / 20)
    completeness = 0.5 * chars_score + 0.5 * clause_score

    # 准确度：乱码率（0 好，>0.05 差）+ 常用汉字占比（>0.4 好）+ GT 相似率（>0.7 好）
    garble = metrics.get("garble_ratio") or 0.0
    garble_score = max(0.0, 1.0 - garble * 20)  # 0→1，0.05→0
    cn_score = min(1.0, (metrics.get("chinese_common_ratio") or 0.0) / 0.4)
    gt = metrics.get("gt_similarity")
    gt_score = min(1.0, (gt or 0.0) / 0.7) if gt is not None else 0.5  # 无 GT 时给中性分
    accuracy = 0.4 * garble_score + 0.3 * cn_score + 0.3 * gt_score

    # 结构：条款数多 = 结构信号强（简化）；真实结构需 adapter 后算
    structure = min(1.0, (metrics.get("clause_count") or 0) / 15)

    # 速度：默认 1（无耗时信息）；有耗时则 30s 内满分，越长越低
    if time_cost_s <= 0:
        speed = 0.7  # 未知耗时给中性偏上
    else:
        speed = max(0.1, 1.0 - time_cost_s / 300)

    score = 100 * (
        0.30 * completeness + 0.30 * accuracy + 0.25 * structure + 0.15 * speed
    )
    return round(max(0.0, min(100.0, score)), 2)


def build_route_table(reports: list[dict]) -> dict:
    """按文档类型输出推荐引擎路由表。

    Args:
        reports: [{doc_type, engine, score, pages, est_time_s}, ...]
    """
    by_doc: dict[str, dict] = {}
    for r in reports:
        doc_type = r.get("doc_type", "unknown")
        engine = r.get("engine")
        score = r.get("score", 0.0)
        if doc_type not in by_doc or score > by_doc[doc_type].get("score", 0.0):
            by_doc[doc_type] = {
                "recommended": engine,
                "score": score,
                "pages": r.get("pages"),
                "est_time_s": r.get("est_time_s"),
            }
    return by_doc


def assert_same_sample_pages(samples: list[dict]) -> None:
    """两个引擎必须跑相同页集（页数/样本一致），否则报错。"""
    seen: dict[str, set] = {}
    for s in samples:
        key = (s.get("doc"), s.get("engine"))
        pages = frozenset(s.get("pages") or [])
        if key[1] not in seen:
            seen[key[1]] = set()
        # 同引擎跨样本页集合合并记录
    # 简版：要求每个文档的 rapid 与 mineru 页数一致
    per_doc: dict[str, dict[str, int]] = {}
    for s in samples:
        d, e = s.get("doc"), s.get("engine")
        per_doc.setdefault(d, {})[e] = len(s.get("pages") or [])
    for d, engines in per_doc.items():
        counts = set(engines.values())
        if len(counts) > 1:
            raise ValueError(
                f"文档 {d} 的引擎页数不一致: {engines}（bake-off 必须跑相同页集）"
            )
