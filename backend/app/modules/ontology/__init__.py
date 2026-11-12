"""本体（Ontology RAG）HTTP 接口层。

- routes：管理端 CRUD / 参数推荐卡 / 参数表导入 / profile 热重载
- schemas：输入输出结构
"""
from app.modules.ontology.routes import router

__all__ = ["router"]
