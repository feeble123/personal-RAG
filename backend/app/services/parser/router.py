"""解析路由（P1-2 单元D）：决定 PDF 用哪个引擎。

bake-off 结论（RAG-OPTIMIZATION 记录）：
- 文字层 PDF：走 PyMuPDF 文字层路径（0.09s，质量最高）
- 扫描 PDF：MinerU 更快更准（快 3-4 倍）；RapidOCR 作低成本默认
- 文档级路由（整份扫描文档统一走一个引擎，避免反复加载 2.6GB 模型）

本模块是纯决策函数（输入页面预检结果，输出引擎决策），便于离线单测。
"""
from __future__ import annotations

from dataclasses import dataclass, field

# 引擎名
ENGINE_TEXT = "text"      # PyMuPDF 文字层
ENGINE_RAPID = "rapid"    # RapidOCR（扫描页默认）
ENGINE_MINERU = "mineru"  # MinerU（扫描页高质量）


@dataclass
class RouterPolicy:
    """路由策略（来自 settings）。"""

    mineru_enabled: bool = False
    scan_engine: str = "rapid"        # rapid / mineru / auto
    mineru_min_scan_ratio: float = 0.5  # 扫描占比阈值
    min_scan_pages: int = 2           # 至少 N 页扫描才值得上 MinerU（单页扫描不值得加载模型）


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
    policy: RouterPolicy | None = None,
) -> EngineDecision:
    """文档级路由：根据扫描页占比决定引擎。

    Args:
        total_pages: 总页数
        scanned_pages: 扫描页数（无文字层页）
        policy: 路由策略（默认从 settings 读）
    """
    if policy is None:
        policy = _build_policy_from_settings()

    decision = EngineDecision()
    scan_ratio = scanned_pages / total_pages if total_pages else 0.0

    # 纯文字层（无扫描页）→ 文字层路径
    if scanned_pages == 0:
        decision.doc_level = ENGINE_TEXT
        decision.reasons.append(f"无扫描页（{scanned_pages}/{total_pages}）→ 文字层")
        return decision

    # 有扫描页：默认 RapidOCR
    decision.doc_level = ENGINE_RAPID
    decision.reasons.append(f"扫描页 {scanned_pages}/{total_pages}（占比 {scan_ratio:.0%}）")

    # MinerU 条件：启用 + 引擎允许 + 扫描占比达标 + 扫描页数足够
    want_mineru = policy.mineru_enabled and policy.scan_engine in ("mineru", "auto")
    ratio_ok = scan_ratio >= policy.mineru_min_scan_ratio
    pages_ok = scanned_pages >= policy.min_scan_pages
    if want_mineru and ratio_ok and pages_ok:
        decision.doc_level = ENGINE_MINERU
        decision.use_mineru = True
        decision.reasons.append(
            f"扫描占比 {scan_ratio:.0%} ≥ {policy.mineru_min_scan_ratio:.0%} "
            f"且 {scanned_pages}页 ≥ {policy.min_scan_pages} → MinerU"
        )
    else:
        decision.reasons.append("MinerU 条件未满足（启用/占比/页数）→ 保持 RapidOCR")

    return decision
