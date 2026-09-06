"""Environment-driven settings. No secrets are hardcoded anywhere else in the app."""
import os

from dotenv import load_dotenv

load_dotenv()


class Settings:
    database_url: str = os.getenv("DATABASE_URL", "postgresql://shiptrack:shiptrack@localhost:5432/shiptrack")

    llm_mode: str = os.getenv("LLM_MODE", "auto")  # auto | local | foundry | mock

    # Local OpenAI-compatible server (Ollama by default: `ollama pull gpt-oss:20b`).
    local_base_url: str | None = os.getenv("LOCAL_BASE_URL") or None
    local_model: str = os.getenv("LOCAL_MODEL", "gpt-oss:20b")
    local_api_key: str = os.getenv("LOCAL_API_KEY", "ollama")  # value is ignored by Ollama, just needs to be non-empty

    # Microsoft/Azure AI Foundry (kept as an alternative backend).
    foundry_endpoint: str | None = os.getenv("FOUNDRY_ENDPOINT") or None
    foundry_api_key: str | None = os.getenv("FOUNDRY_API_KEY") or None
    foundry_deployment: str = os.getenv("FOUNDRY_DEPLOYMENT", "gpt-4o-mini")
    foundry_api_version: str = os.getenv("FOUNDRY_API_VERSION", "2024-10-21")

    app_port: int = int(os.getenv("APP_PORT", "8000"))
    log_level: str = os.getenv("LOG_LEVEL", "INFO")


settings = Settings()
