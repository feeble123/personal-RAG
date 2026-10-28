"""单元 2：把已有知识库/文档补上「业务域」和「文档类型」。

默认 **dry-run**（只打印计划，不改库）；确认无误后加 `--apply` 才写。

映射按**知识库名称**匹配（不用 id，换环境也稳），未命中映射的库保持 general、
文档类型保持原值。

用法（在 backend 目录）：
    .venv\\Scripts\\python.exe -X utf8 scripts\\backfill_classification.py          # 预览
    .venv\\Scripts\\python.exe -X utf8 scripts\\backfill_classification.py --apply  # 执行
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from sqlalchemy import select  # noqa: E402

from app.core.taxonomy import (  # noqa: E402
    DOMAIN_LABELS,
    DOC_TYPE_LABELS,
    normalize_doc_type,
    normalize_domain,
)
from app.db.models import Document, KnowledgeBase  # noqa: E402
from app.db.session import async_session_factory  # noqa: E402

# 知识库名称 → (业务域, 文档类型, 归类理由)
# 说明：一个库一个域（用户 2026-10-28 确认）；库内文档类型按文档性质定。
MAPPING: dict[str, tuple[str, str, str]] = {
    "水利工程基础": ("general", "textbook", "工程水文/水力学讲义"),
    "数字孪生水利导则": ("general", "standard", "信息化技术导则"),
    "数字孪生农村供水工程建设技术指南（试行）": ("water", "standard", "农村供水工程建设指南"),
    "水利技术标准编写规定": ("general", "standard", "标准编写规定"),
    "重庆市防汛抗旱应急预案": ("flood", "emergency_plan", "省级防汛抗旱预案"),
    "SL 483-2017 洪水风险图编制导则": ("flood", "standard", "洪水风险图行业标准"),
    "GB 38509-2020 滑坡防治设计规范": ("construction", "standard", "滑坡防治设计规范"),
    "高支模专项施工方案": ("construction", "scheme", "专项施工方案"),
    "已报送方案台账": ("construction", "ledger", "方案报送台账"),
    "四方井仓库及管理用房主要电气设备材料表": ("construction", "ledger", "设备材料清单"),
    "GB 50332给水排水工程管道结构设计规范": ("water", "standard", "给排水管道设计规范"),
    "水力学第5版": ("general", "textbook", "高校教材"),
    "水资源规划及利用": ("water", "textbook", "水资源专业教材"),
    "水资源规划及利用-前100页验证": ("water", "textbook", "同上（验证用片段）"),
    "GB50201-2014 防洪标准": ("flood", "standard", "防洪国家标准"),
}


async def run(apply: bool) -> int:
    async with async_session_factory() as db:
        kbs = (await db.execute(select(KnowledgeBase).order_by(KnowledgeBase.id))).scalars().all()
        if not kbs:
            print("数据库里没有知识库，无需回填。")
            return 0

        print("=" * 100)
        print("单元 2 分类回填" + ("（执行模式 --apply）" if apply else "（预览模式，不改库）"))
        print("=" * 100)
        print(f"{'ID':>4}  {'知识库':<32}{'业务域':<22}{'文档类型':<12}文档数  说明")
        print("-" * 100)

        matched = 0
        for kb in kbs:
            doc_count = (
                await db.execute(select(Document.id).where(Document.kb_id == kb.id))
            ).scalars().all()
            key = kb.name.strip()
            mapping = MAPPING.get(key) or MAPPING.get(kb.name)
            if mapping:
                matched += 1
                domain, doc_type, note = mapping
            else:
                domain, doc_type, note = kb.business_domain or "general", "", "未在映射表内，保持原状"

            domain = normalize_domain(domain)
            domain_text = f"{DOMAIN_LABELS[domain]}（{domain}）"
            type_text = DOC_TYPE_LABELS.get(doc_type, "保持原值") if doc_type else "保持原值"
            mark = "" if mapping else "  ← 未匹配"
            print(
                f"{kb.id:>4}  {kb.name[:30]:<32}{domain_text:<22}{type_text:<12}"
                f"{len(doc_count):>4}    {note}{mark}"
            )

            if apply:
                kb.business_domain = domain
                if doc_type:
                    for doc in (
                        await db.execute(select(Document).where(Document.kb_id == kb.id))
                    ).scalars().all():
                        doc.doc_type = normalize_doc_type(doc_type)
                print(f"      → 已更新知识库 #{kb.id}")

        print("-" * 100)
        print(f"共 {len(kbs)} 个知识库，命中映射 {matched} 个，未匹配 {len(kbs) - matched} 个")

        if apply:
            await db.commit()
            print("已提交到数据库。")
        else:
            print("这是预览，没有改库。确认后加 --apply 执行。")
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="单元 2：业务域 / 文档类型回填")
    parser.add_argument("--apply", action="store_true", help="真正写库（默认只预览）")
    args = parser.parse_args()
    return asyncio.run(run(args.apply))


if __name__ == "__main__":
    sys.exit(main())
