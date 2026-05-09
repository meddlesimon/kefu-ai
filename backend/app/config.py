from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    wxwork_corpid: str
    wxwork_agentid: str
    wxwork_app_secret: str
    wxwork_archive_secret: str
    wxwork_private_key_path: str = "/app/secrets/private_key.pem"

    app_port: int = 8098
    domain: str = "kefu.sunyeupupup.com"

    jwt_secret: str
    jwt_ttl_seconds: int = 28800

    # 会话存档拉取
    db_path: str = "/app/data/chat.db"
    pull_interval_seconds: int = 5
    sdk_lib_path: str = "/app/lib/libWeWorkFinanceSdk_C.so"

    # RAG (阿里百炼)
    aliyun_api_key: str = ""
    aliyun_embed_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    aliyun_embed_model: str = "text-embedding-v4"
    aliyun_embed_dim: int = 1024
    aliyun_llm_model: str = "qwen-plus"
    lancedb_path: str = "/app/data/lancedb"

    # AI 生成 (DeepSeek)
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com/v1"
    deepseek_model: str = "deepseek-v4-flash"

    # AI 生成 (豆包/火山方舟,OpenAI 兼容)
    doubao_api_key: str = ""
    doubao_base_url: str = "https://ark.cn-beijing.volces.com/api/v3"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )


settings = Settings()
