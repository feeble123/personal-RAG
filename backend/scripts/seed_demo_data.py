"""演示数据种子脚本：创建「水利工程基础」知识库并入库 data/demo_docs/ 下的示例文档。

用法（backend 目录下）：
    .venv/Scripts/python.exe scripts/seed_demo_data.py

说明：
- 无需启动服务器，直接调用内部服务完成入库
- 需要配置好 .env 中的 EMBEDDING_API_KEY（或临时设 EMBEDDING_PROVIDER=fake 离线演示）
- 幂等：同名知识库已存在则跳过
"""
from __future__ import annotations

import asyncio
import logging
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

# 确保可导入 backend/app 包（无论从哪个目录运行）
BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s | %(message)s")
logger = logging.getLogger("seed")

DEMO_DIR = BASE_DIR / "data" / "demo_docs"
KB_NAME = "水利工程基础"


async def main() -> None:
    from sqlalchemy import select

    from app.core.config import settings
    from app.db.models import Document, KnowledgeBase
    from app.db.session import async_session_factory, init_db
    from app.modules.ingestion import manager as ingestion

    await init_db()
    settings.upload_dir_path.mkdir(parents=True, exist_ok=True)

    async with async_session_factory() as db:
        kb = await db.scalar(select(KnowledgeBase).where(KnowledgeBase.name == KB_NAME))
        if kb:
            logger.info("知识库「%s」已存在，跳过创建", KB_NAME)
        else:
            kb = KnowledgeBase(name=KB_NAME, description="水利工程大学基础课程资料（水力学 / 工程水文学 / 水工建筑物）")
            db.add(kb)
            await db.commit()
            await db.refresh(kb)
            logger.info("已创建知识库「%s」 id=%s", KB_NAME, kb.id)

        # 已入库文档去重（同名文件不重复入库）
        existing = set(
            (await db.scalars(select(Document.filename).where(Document.kb_id == kb.id))).all()
        )

        for src in sorted(DEMO_DIR.glob("*.md")):
            if src.name in existing:
                logger.info("文档「%s」已入库，跳过", src.name)
                continue
            # 复制到 uploads 目录（生成存储文件名）
            stored_name = f"{datetime.now(timezone.utc).strftime('%H%M%S%f')}_{src.name}"
            dest = settings.upload_dir_path / stored_name
            shutil.copy2(src, dest)
            doc = Document(
                kb_id=kb.id,
                filename=src.name,
                stored_path=stored_name,
                file_type="md",
                file_size=src.stat().st_size,
                status="pending",
            )
            db.add(doc)
            await db.commit()
            await db.refresh(doc)
            logger.info("入库中：%s (doc_id=%s)", src.name, doc.id)
            await ingestion._process_document(doc.id)  # noqa: SLF001  直接 await 完成
            logger.info("完成：%s", src.name)

    logger.info("演示数据准备完成。启动系统后可用管理员账号上传更多文档，或直接问答。")
    logger.info("推荐测试问题：明渠均匀流的形成条件是什么？设计洪水如何推求？")


if __name__ == "__main__":
    asyncio.run(main())
