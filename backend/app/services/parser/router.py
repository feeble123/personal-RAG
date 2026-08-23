"""解析路由（P1-2 单元D + 修正）：决定 PDF 用哪个引擎。

bake-off 结论：
- 文字层 PDF：走 PyMuPDF 文字层路径（0.09s，质量最高）
- 扫描 PDF：MinerU 更快更准（快 3-4 倍）
- 公式多 / 表格多 / 图片多的 PDF：PyMuPDF 恢复差（ρ→p、表格炸、图文分离）→ MinerU

路由信号（多维度复杂度，任一命中走 MinerU）：
1. 扫描占比高（已有）
2. 公式符号密度高（≈≥≤∑∫∂√ 等）
3. 表格页占比高（find_tables）
4. 图片页占比高（get_images）

只有「纯文字层 + 无公式 + 无表格 + 无图片」的简单文档走 PyMuPDF 快通道。

本模块是纯决策函数（输入页面预检结果，输出引擎决策），便于离线单测。
"""
from __future__ import annotations

from dataclasses import dataclass, field

# 引擎名
ENGINE_TEXT = "text"      # PyMuPDF 文字层（简单文档快通道）
ENGINE_RAPID = "rapid"    # RapidOCR（扫描页低成本默认）
ENGINE_MINERU = "mineru"  # MinerU（复杂 PDF 高质量）


@dataclass
class RouterPolicy:
    """路由策略（来自 settings）。"""

    mineru_enabled: bool = False
    scan_engine: str = "rapid"        # rapid / mineru / auto
    mineru_min_scan_ratio: float = 0.5  # 扫描占比阈值
    min_complex_pages: int = 2        # 至少 N 页复杂（公式/表格/图片）才上 MinerU
    # 公式符号：每页平均含这些符号的字符数超过阈值 → 公式页
    formula_symbol_chars_per_page: float = 3.0
    # 表格页 / 图片页占比阈值
    complex_page_ratio: float = 0.15


@dataclass
class EngineDecision:
    """路由决策结果。"""

    doc_level: str = ENGINE_TEXT       # 文档级引擎（text/rapid/mineru）
    use_mineru: bool = False           # 是否整文档走 MinerU
    reasons: list[str] = field(default_factory=list)


def _build_policy_from_settings() -> RouterPolicy:
    from app.core.config import settings

    return RouterPolicy(
        mineru_enabled=settings.mineru_enabled,
        scan_engine=settings.pdf_scan_engine,
        mineru_min_scan_ratio=settings.mineru_min_scan_ratio,
    )


def route_pdf(
    *,
    total_pages: int,
    scanned_pages: int,
    formula_pages: int = 0,
    table_pages: int = 0,
    image_pages: int = 0,
    policy: RouterPolicy | None = None,
) -> EngineDecision:
    """文档级路由：多维度复杂度检测决定引擎。

    Args:
        total_pages: 总页数
        scanned_pages: 扫描页数（无文字层页）
        formula_pages: 含公式符号的页数
        table_pages: 含表格的页数
        image_pages: 含图片的页数
        policy: 路由策略（默认从 settings 读）
    """
    if policy is None:
        policy = _build_policy_from_settings()

    decision = EngineDecision()
    if total_pages <= 0:
        decision.reasons.append("无效文档（0 页）→ 文字层")
        return decision

    scan_ratio = scanned_pages / total_pages
    complex_pages = max(formula_pages, table_pages, image_pages)

    # ---- 复杂度信号汇总 ----
    signals: list[str] = []
    if scanned_pages:
        signals.append(f"扫描 {scanned_pages}/{total_pages} ({scan_ratio:.0%})")
    if formula_pages:
        signals.append(f"公式页 {formula_pages}")
    if table_pages:
        signals.append(f"表格页 {table_pages}")
    if image_pages:
        signals.append(f"图片页 {image_pages}")

    # ---- 决策 ----
    # MinerU 启用 + 引擎允许 + 任一复杂度信号达标
    want_mineru = policy.mineru_enabled and policy.scan_engine in ("mineru", "auto")

    # 信号 1：扫描占比高
    scan_ok = scanned_pages >= policy.min_complex_pages and scan_ratio >= policy.mineru_min_scan_ratio
    # 信号 2/3/4：复杂页（公式/表格/图片）占比高
    complex_ok = (
        complex_pages >= policy.min_complex_pages
        and complex_pages / total_pages >= policy.complex_page_ratio
    )

    if want_mineru and (scan_ok or complex_ok):
        decision.doc_level = ENGINE_MINERU
        decision.use_mineru = True
        reasons = "; ".join(signals) if signals else "复杂内容"
        decision.reasons.append(f"{reasons} → MinerU（版面/公式/表格理解）")
        return decision

    # 有扫描页但条件不足 → RapidOCR
    if scanned_pages:
        decision.doc_level = ENGINE_RAPID
        decision.reasons.append(f"扫描 {scanned_pages}/{total_pages}，MinerU 条件未满足 → RapidOCR")
        return decision

    # 纯文字层 + 无复杂信号 → PyMuPDF 快通道
    decision.doc_level = ENGINE_TEXT
    if signals:
        decision.reasons.append(f"{'; '.join(signals)} 但占比低 → 文字层（PyMuPDF）")
    else:
        decision.reasons.append(f"纯文字层（{total_pages}页）→ 文字层")
    return decision
