"""本体（Ontology RAG）HTTP 接口层。

- routes.router        ：管理端 CRUD / 参数推荐卡 / 参数表导入 / profile 热重载（管理员 JWT）
- routes.public_router ：对外检索与问答（X-API-Key，给自动建模 agent 用）
- schemas              ：输入输出结构
"""
from app.modules.ontology.routes import public_router, router

__all__ = ["public_router", "router"]
