from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://fastsim:fastsim@localhost:5432/fastsim"
    jwt_secret: str = "dev-secret"
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 10080
    cors_origins: str = "http://localhost:5173"
    payment_provider: str = "disabled"
    # Keep mock as the default until eSIM Access credentials and plan mappings
    # have been verified with a refundable live test order.
    esim_provider: str = "mock"
    esimaccess_base_url: str = "https://api.esimaccess.com"
    esimaccess_access_code: str = ""
    esimaccess_secret_key: str = ""
    esimaccess_webhook_token: str = ""
    esimaccess_timeout_seconds: int = 20
    cbu_currency_url: str = "https://cbu.uz/uz/arkhiv-kursov-valyut/json/"
    currency_rate_cache_seconds: int = 21600
    # Used only when the Central Bank endpoint is temporarily unavailable.
    uzs_per_usd_fallback: float = 12000
    # Kept server-side only. Never expose Telegram credentials to the storefront.
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    telegram_timeout_seconds: int = 10
    support_message_cooldown_seconds: int = 30

    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


settings = Settings()
