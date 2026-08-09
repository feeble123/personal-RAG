"""应用配置：读取 .env（pydantic-settings）。

所有第三方服务（DeepSeek / 硅基流动 embedding / 数据库 / 向量库）均在此配置，
通过 .env 即可切换厂商，实现「升级路径」而无需改代码。
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# 项目根目录（backend/）
BASE_DIR = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ---- 应用 ----
    app_name: str = "水利知识库问答系统"
    debug: bool = False
    host: str = "0.0.0.0"
    port: int = 8000
    # CORS 允许的来源（前端开发服务器）
    cors_origins: list[str] = [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:8000",
        "http://127.0.0.1:8000",
    ]

    # ---- 安全 ----
    # 生产环境务必通过 .env 覆盖
    jwt_secret: str = "dev-secret-change-me-in-production"
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 60 * 24 * 7  # 7 天
    admin_username: str = "admin"
    admin_password: str = "123456"

    # ---- 数据库 (SQLite 零安装；换 MySQL/Postgres 改连接串即可) ----
    database_url: str = f"sqlite+aiosqlite:///{BASE_DIR / 'data' / 'app.db'}"
    db_pool_size: int = 10
    db_max_overflow: int = 20

    # ---- 上传 ----
    max_upload_size: int = 200 * 1024 * 1024  # 200MB
    upload_dir: str = str(BASE_DIR / "data" / "uploads")
    # 与解析器注册表保持一致（doc 旧格式需另存为 docx；xls 需另存为 xlsx）
    allowed_extensions: list[str] = ["pdf", "docx", "md", "markdown", "txt", "xlsx", "csv"]

    # ---- 向量库 (Chroma 嵌入式) ----
    chroma_dir: str = str(BASE_DIR / "data" / ".chroma")
    chroma_collection: str = "kb_chunks"
    top_k_vector: int = 50      # 向量检索候选
    top_k_bm25: int = 50        # BM25 检索候选
    top_k_rrf: int = 20         # 融合后候选
    top_k_final: int = 5        # 最终进入 prompt 的引用数
    # ---- 重排（bge-reranker API，解决 BGE-M3 向量对部分查询区分度不足）----
    rerank_enabled: bool = True
    rerank_model: str = "BAAI/bge-reranker-v2-m3"
    # 送入 rerank 的候选数：BGE-M3 对部分查询（抽象问法/长句）会漏召回正确切片，
    # 候选池过小会让 reranker 根本见不到正确答案。扩到 100 让质量关卡覆盖更宽。
    rerank_candidates: int = 100
    # 引用候选最小内容长度（过滤封面/目录等低信息量短块）
    min_content_len: int = 40
    # HNSW 参数（Chroma 1.0 configuration）
    hnsw_ef_construction: int = 200
    hnsw_max_neighbors: int = 32
    hnsw_ef_search: int = 100
    hnsw_space: str = "cosine"

    # ---- 分块 ----
    chunk_size: int = 512
    chunk_overlap: int = 50

    # ---- Embedding（OpenAI 兼容，默认硅基流动免费 BGE-M3）----
    embedding_provider: str = "openai_compatible"
    embedding_base_url: str = "https://api.siliconflow.cn/v1"
    embedding_api_key: str = ""
    embedding_model: str = "BAAI/bge-m3"
    # BGE-M3 训练时无指令前缀；若换带前缀的模型（如 bge-large-zh-v1.5）在 .env 打开
    embedding_query_instruction: str = ""
    embedding_batch_size: int = 32
    embedding_cache_enabled: bool = True

    # ---- LLM（DeepSeek）----
    llm_provider: str = "deepseek"
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com/v1"
    llm_model: str = "deepseek-chat"
    llm_temperature: float = 0.2
    # 输出 token 上限：DeepSeek-chat 输出天花板 8192。曾设 2000 导致「36份台账」这类长表
    # 每次在第 17~23 行截断（根因），拉到上限后完整表格可一次生成；普通短答模型自然收尾。
    llm_max_tokens: int = 8192
    llm_max_retries: int = 3
    llm_timeout: int = 120

    # ---- 问答 ----
    # 语义缓存：命中余弦阈值以上直接回缓存答案
    semantic_cache_threshold: float = 0.92
    semantic_cache_enabled: bool = True
    # 语义缓存检索池（最近 N 条做余弦比对）与容量上限（超限淘汰最旧）
    semantic_cache_pool: int = 200
    semantic_cache_max_entries: int = 500
    # 会话历史注入条数（多轮记忆）
    history_turns: int = 6

    # ---- 问答记忆库（AI native 自身长库，用户背书数据）----
    memory_enabled: bool = True
    memory_threshold: float = 0.93        # 严格复用阈值：近似同题（主题一致）才直接复用记忆答案
    memory_max_entries: int = 300         # 每用户每 kb 作用域的记忆条数上限
    memory_pool: int = 100                # 召回候选池（最近 N 条做余弦比对）
    memory_eviction_ratio: float = 0.2    # 容量超限时淘汰最旧的比例

    # ---- 证据等级（检索质量判级，U3）----
    # 依据 rerank 后 top1 分数 + 强相关块数判定四级证据质量：
    #   sufficient 充足 / partial 部分 / weak 较弱 / none 不足
    # 动态放行：仅「实时/外部信息」类问题（天气/时间/新闻等）且证据不强时拒答；
    # 其余（问候/闲聊/能力咨询/规范概述/领域问答）一律放行，由 LLM 诚实作答。
    evidence_strong_threshold: float = 0.6      # 强相关块分数阈值（>= 该分块数 >= 2 → 交叉印证充足）
    evidence_sufficient_threshold: float = 0.8  # top1 单块达到该分 → 充足
    evidence_partial_threshold: float = 0.5     # top1 达到该分 → 部分
    evidence_weak_threshold: float = 0.3        # top1 达到该分 → 较弱；低于该分 → 不足
    # 完整性扩展：枚举/清单类问题（完整/所有/全部/名单…）拉取整个列表章节切片的上限，
    # 保证「专家名单」等多页列表类回答不遗漏成员（每次漏一部分的根因是 top_k 只覆盖部分页）
    complete_expansion_cap: int = 40
    # ---- 答案校验（层2/层3）----
    # LLM 完备性校验默认关闭（opt-in）：用户对回答不满意时，通过回答气泡上的
    # 「🤖 LLM优化」按钮触发 /optimize（整文档扩展证据 + 补全要求重生成 + 校验循环）。
    # 如需恢复自动校验（每次枚举题生成后自动补全重生成），在 .env 置 ANSWER_VERIFY_ENABLED=true。
    answer_verify_enabled: bool = False
    # 校验 LLM 的最大输出 token（校验只需简短判定，控制成本）
    answer_verify_max_tokens: int = 200
    # /optimize 补全重生成的最大尝试次数（每次先校验，仍不完整再带「补全要求」重生成）
    answer_verify_max_retries: int = 2

    # ---- 限流 ----
    auth_rate_limit: str = "10/minute"    # 注册/登录（按 IP）
    chat_rate_limit: str = "60/minute"    # 问答（按用户）
    feedback_rate_limit: str = "30/minute"  # 反馈点赞/踩（按用户）

    # ---- OCR / PDF 质量检测（默认 RapidOCR；可选 paddle）----
    ocr_engine: str = "rapid"
    # OCR 渲染分辨率：200dpi 对印刷中文足够，速度约为 300dpi 的 2 倍
    ocr_dpi: int = 200
    # onnxruntime 单会话推理线程数：设低让线程池并行 OCR 提速（默认全核反而互相争抢）
    ocr_intra_op_threads: int = 2
    # OCR 分条带数：整页检测模型在密集排版下偶发漏行（实测漏条款行），
    # 拆成 N 个横向重叠条带分别识别再合并可避免漏行。=1 关闭分条带。
    ocr_tiles: int = 3
    # 扫描页判定：单页有效文本字符数低于该阈值视为扫描/图片页
    pdf_text_threshold_per_page: int = 40
    # 乱码判定：替换字符 � 占比阈值
    garble_threshold: float = 0.02
    # 乱码判定：常用汉字占比阈值（CID 字体 ToUnicode 损坏时中文被映射成生僻字，
    # 如「犮犪狊…」=custom…，无替换符/私用区字符；正常中文正文常用字占比实测 0.4+，
    # 乱码页 ≈0。低于该阈值 → 该页转 OCR）
    chinese_common_threshold: float = 0.2

    @property
    def upload_dir_path(self) -> Path:
        return Path(self.upload_dir)

    @property
    def chroma_dir_path(self) -> Path:
        return Path(self.chroma_dir)

    @property
    def data_dir(self) -> Path:
        return BASE_DIR / "data"


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
