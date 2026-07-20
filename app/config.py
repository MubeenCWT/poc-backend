from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore",
    )

    DATABASE_URL: str = "sqlite:///./uae_realestate.db"
    JWT_SECRET: str
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRE_MINUTES: int = 720

    # WhatsApp via the Meta WhatsApp Cloud API (test number)
    ADMIN_WHATSAPP_NUMBER: str = ""   # admin recipient, country code, e.g. +9715XXXXXXXX
    OWNER_WHATSAPP_NUMBER: str = ""   # optional seed/demo owner WhatsApp number
    META_ACCESS_TOKEN: str = ""
    META_PHONE_NUMBER_ID: str = ""
    META_API_VERSION: str = "v21.0"
    META_VERIFY_TOKEN: str = ""       # webhook verification token (matches Meta dashboard)

    # Used by the chatbot graph when calling back into this API.
    # Leave empty to auto-detect http://localhost:$PORT (works on Railway).
    API_BASE_URL: str = ""

    LLM_API_BASE: str = ""
    LLM_API_KEY: str = ""
    LLM_MODEL: str = ""


settings = Settings()
