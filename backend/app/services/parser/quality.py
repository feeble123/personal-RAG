"""P1-3 质量门禁：多特征质量评分（纯函数，可单测）。

在 pdf.py 现有 needs_review 基础上，引入可计算的质量评分：
- 特征：OCR 占比 / 平均置信度 + 分页置信方差 / 乱码率 / 文本量 / 表格爆炸率 / 目录条目
- 产出 score ∈ [0,100] + reasons；score < threshold → needs_review
- 关键：捕获「伪高置信」——OCR 置信 0.95 但表格块字符占比超高（表格识别炸了）
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class QualityPolicy:
    """质量门禁阈值（来自 settings）。"""

    garble_threshold: float = 0.02
    review_threshold: float = 60.0      # score 低于此 → needs_review
    min_total_chars: int = 500          # 文本量下限
    table_explosion_ratio: float = 0.5  # 表格块字符占比 > 此 → 表格异常
    min_ocr_confidence: float = 0.5     # 平均置信度下限


def build_policy_from_settings() -> QualityPolicy:
    from app.core.config import settings

    return QualityPolicy(
        garble_threshold=settings.garble_threshold,
        min_total_chars=500,
        min_ocr_confidence=0.5,
    )


def compute_quality_score(quality: dict, *, policy: QualityPolicy | None = None) -> tuple[float, list[str]]:
    """多特征质量评分 → (score[0,100], reasons)。

    quality 来自 pdf.py parse() 的 quality dict。
    """
    if policy is None:
        policy = build_policy_from_settings()

    reasons: list[str] = []
    total_pages = quality.get("pages") or (quality.get("ocr_pages", 0) + quality.get("text_pages", 0)) or 1
    ocr_pages = quality.get("ocr_pages", 0)
    text_pages = quality.get("text_pages", 0)
    ocr_ratio = ocr_pages / total_pages if total_pages else 0.0
    mean_conf = quality.get("mean_ocr_confidence")
    garble = quality.get("garble_ratio", 0.0)
    total_chars = quality.get("total_chars", 0)
    tables = quality.get("tables", 0)
    table_chars = quality.get("table_chars", 0) or _estimate_table_chars(quality)
    table_ratio = table_chars / total_chars if total_chars else 0.0

    # 各项打分（每项 0-1，加权求和）
    scores: dict[str, float] = {}

    # 1) 完整度（30%）：文本量是否足够
    completeness = min(1.0, total_chars / (policy.min_total_chars * 2))
    scores["completeness"] = completeness
    if total_chars < policy.min_total_chars:
        reasons.append(f"文本量过少 ({total_chars} 字)")

    # 2) 乱码（25%）：乱码率越低越好
    garble_score = max(0.0, 1.0 - garble / (policy.garble_threshold * 4))
    scores["garble"] = garble_score
    if garble > policy.garble_threshold * 2:
        reasons.append(f"乱码率过高 (garble={garble:.3f})")

    # 3) OCR 质量（20%）：置信度 + 分页方差
    if ocr_ratio > 0:
        conf_score = 0.0
        if mean_conf is not None:
            conf_score = min(1.0, mean_conf / 1.0)
            if mean_conf < policy.min_ocr_confidence:
                reasons.append(f"OCR 平均置信度低 (mean={mean_conf:.2f})")
        # 分页置信方差：conf 列表里低置信页占比
        confs = quality.get("ocr_confidence") or []
        if confs:
            low_ratio = sum(1 for c in confs if c < policy.min_ocr_confidence) / len(confs)
            conf_score *= (1.0 - low_ratio * 0.5)  # 低置信页多 → 打折
            if low_ratio > 0.5:
                reasons.append(f"低置信页占比高 ({low_ratio:.0%})")
    else:
        conf_score = 1.0  # 无 OCR（纯文字层）→ 满分
    scores["ocr"] = conf_score

    # 4) 表格异常（15%）：表格块字符占比超高 → 伪高置信
    table_score = 1.0
    if table_ratio > policy.table_explosion_ratio and total_chars > policy.min_total_chars:
        table_score = max(0.0, 1.0 - (table_ratio - policy.table_explosion_ratio) * 2)
        reasons.append(f"表格字符占比过高 ({table_ratio:.0%}，疑似表格识别爆炸)")
    scores["table"] = table_score

    # 5) 结构（10%）：表格存在是正信号（除非爆炸），OCR 页过多是负信号
    structure = 0.5 + 0.5 * min(1.0, tables / 5) if tables else 0.5
    structure -= 0.3 * min(1.0, ocr_ratio)  # OCR 占比高 → 结构信号弱
    scores["structure"] = max(0.0, structure)

    score = 100 * (
        0.30 * completeness
        + 0.25 * garble_score
        + 0.20 * conf_score
        + 0.15 * table_score
        + 0.10 * structure
    )
    return round(max(0.0, min(100.0, score)), 2), reasons


def _estimate_table_chars(quality: dict) -> int:
    """估算表格块字符量：无 table_chars 时用 tables 数粗估（每表 ~200 字）。"""
    return quality.get("tables", 0) * 200


def is_review_required(score: float, reasons: list[str], policy: QualityPolicy | None = None) -> bool:
    """score 低于阈值 或 有明确严重 reason → needs_review。"""
    if policy is None:
        policy = build_policy_from_settings()
    if score < policy.review_threshold:
        return True
    return any(
        "乱码率过高" in r or "文本量过少" in r or "OCR 平均置信度低" in r or "表格字符占比过高" in r
        for r in reasons
    )
