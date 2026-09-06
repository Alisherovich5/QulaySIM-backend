"""Application configuration.

Every security-critical value is REQUIRED — there are deliberately no
fall-back defaults for secrets. A missing value must crash the process at
import time rather than silently boot an insecure service.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, PostgresDsn, RedisDsn, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", case_sensitive=False
    )

    # --- Runtime -----------------------------------------------------------
    environment: Literal["local", "staging", "production"] = "local"
    debug: bool = False
    log_level: str = "INFO"
    service_name: str = "qulaysim-api"

    # --- Datastores (required) --------------------------------------------
    database_url: PostgresDsn
    redis_url: RedisDsn

    db_pool_size: int = 10
    db_max_overflow: int = 20
    db_pool_recycle_seconds: int = 1800
    db_echo: bool = False

    # --- Auth (required) ---------------------------------------------------
    jwt_secret: Annotated[str, Field(min_length=32)]
    jwt_algorithm: str = "HS256"
    access_token_ttl_minutes: int = 30
    refresh_token_ttl_days: int = 30

    # Shared with the Django admin — both services read the same encrypted
    # columns, so the key must be identical on both sides.
    field_encryption_key: str = ""

    # --- CORS --------------------------------------------------------------
    cors_origins: str = ""

    # --- Rate limits (requests / window seconds) ---------------------------
    # Per-IP windows. Deliberately loose rather than tight: mobile carriers here
    # put hundreds of subscribers behind one NAT address, so a strict per-IP cap
    # does not stop an attacker — it stops a neighbourhood. 5 signups an hour was
    # low enough that ordinary testing locked the form out for a full hour.
    #
    # These still bound abuse: 30 login attempts per 5 minutes is nowhere near
    # enough to brute-force a password of the length the API enforces, and
    # per-account protection is what actually guards a specific customer.
    rate_limit_login: str = "30/300"
    rate_limit_register: str = "20/3600"
    rate_limit_support: str = "3/300"
    # Promo-code attempts, and the two limits do different jobs.
    #
    # Per address is deliberately loose: mobile operators here put thousands of
    # customers behind one CGNAT address, so a tight per-IP ceiling would lock
    # real buyers out of a code they were given because a stranger on the same
    # carrier typed one first. Sixty a minute still makes a script slow.
    #
    # Per signed-in customer is where the tight limit belongs — one person cannot
    # legitimately try ten codes a minute, and an attacker who signs in to
    # enumerate has given us an account to rate-limit and to ban.
    rate_limit_promo_ip: str = "60/60"
    rate_limit_promo: str = "10/60"
    # A page reports at most a handful of metrics; 60 a minute leaves room for a
    # customer opening several tabs without becoming an amplifier.
    rate_limit_rum: str = "60/60"

    # Error reporting. Empty means off — the code paths below are written so an
    # unconfigured install behaves exactly as it did before, because a
    # monitoring dependency that can break a shop is worse than no monitoring.
    sentry_dsn: str = ""
    #: Fraction of requests traced. Errors are always sent; traces cost money.
    sentry_traces_sample_rate: float = 0.0

    # Edge cache. Empty means there is no edge yet, and every call below turns
    # into a no-op — the catalogue's Cache-Control headers already carry
    # `s-maxage`, so the day a CDN is put in front it starts caching without any
    # further change, and this is what tells it a price moved.
    cloudflare_zone_id: str = ""
    cloudflare_api_token: str = ""
    #: Believe `CF-Connecting-IP`. Only true once the origin refuses everything
    #: except Cloudflare's own addresses — until then anybody can send that
    #: header directly and choose which bucket their requests are counted in.
    trust_cloudflare_client_ip: bool = False
    #: How many proxies of our own sit in front of the app, each appending one
    #: entry to X-Forwarded-For. Caddy alone is 1; Cloudflare in front of Caddy
    #: is 2. Wrong by one and every visitor shares a single rate-limit bucket.
    trusted_proxy_hops: int = 1
    rate_limit_default: str = "120/60"

    # --- Cache TTLs --------------------------------------------------------
    cache_ttl_catalog: int = 300
    cache_ttl_landing: int = 300
    cache_ttl_currency: int = 21600

    # --- Providers ---------------------------------------------------------
    payment_provider: Literal["disabled", "mock", "payme", "atmos"] = "disabled"

    # --- Payme (Paycom) ----------------------------------------------------
    # The merchant key authenticates Payme's calls to us. The test key is
    # accepted alongside it so the same deployment can run Payme's sandbox
    # suite without a second environment.
    payme_merchant_id: str = ""
    payme_merchant_key: str = ""
    payme_test_key: str = ""
    payme_checkout_url: str = "https://checkout.paycom.uz"
    # The key inside Payme's `account` object. Must match the merchant cabinet.
    payme_account_field: str = "order_id"
    payme_return_url: str = ""

    # --- ATMOS (hosted checkout + Callback API) ------------------------------
    # The callback api_key is a separate credential from the OAuth pair: ATMOS
    # signs each callback with it, and we never send it anywhere.
    atmos_consumer_key: str = ""
    atmos_consumer_secret: str = ""
    atmos_store_id: int = 0
    atmos_callback_api_key: str = ""
    # Fiscal (OFD) classification code for the eSIM service line items. The
    # business gets this from ATMOS / the tax classifier; invoices are refused
    # without it once fiscalisation is enforced.
    atmos_ikpu_code: str = ""
    atmos_base_url: str = "https://apigw.atmos.uz"
    # ATMOS documents this range as the source of every callback.
    atmos_callback_cidr: str = "92.63.207.0/24"
    # Straight to the eSIM tab, not to the account overview. A customer who has
    # just paid lands here, and the overview opens on the travel globe — so the
    # QR code they came for was one more click away, which is one click too many
    # for somebody standing at an airport.
    atmos_success_url: str = "https://qulaysim.uz/account?tab=esims"
    # Master switch only. Which wholesaler fulfils a given order is decided per
    # plan by the cheapest supplier offer (see app/integrations/suppliers.py);
    # this just says whether real supplier calls happen at all, so "mock" stays
    # the safe default for a deployment with no supplier balance yet.
    #
    # "esimaccess" is the pre-comparison spelling of "live" and is still
    # accepted, because rejecting it would stop the API booting on any host
    # whose .env predates multi-supplier sourcing.
    # Absolute origin of the storefront. Used where a full URL has to be emitted
    # rather than a relative path — the sitemap is the current case.
    public_base_url: str = "https://qulaysim.uz"
    # Public identifier, not a secret — it ships in the page. Empty disables the
    # Google button rather than showing one that cannot work.
    google_client_id: str = ""
    esim_provider: Literal["mock", "live", "esimaccess"] = "mock"
    esimaccess_base_url: str = "https://api.esimaccess.com"
    esimaccess_access_code: str = ""
    esimaccess_secret_key: str = ""
    esimaccess_webhook_token: str = ""
    esimaccess_timeout_seconds: int = 20

    # Wholesalers we have working ordering code for, as a comma-separated list.
    # Checkout refuses a plan no supplier on this list can supply — see
    # app.services.checkout.is_fulfillable. Mirrors the Django setting of the
    # same name; both read the same env var so they cannot disagree about which
    # supplier is connected.
    # Aliased, so the env var really is FULFILLABLE_PROVIDERS rather than
    # FULFILLABLE_PROVIDERS_RAW: two services reading differently-named variables
    # for the same decision is how one of them ends up connected and the other
    # does not.
    fulfillable_providers_raw: str = Field(
        default="esimaccess,esimcard", validation_alias="FULFILLABLE_PROVIDERS"
    )

    # eSIMCard, the second wholesaler. The base URL is NOT esimcard.com: that
    # host now answers every API path with HTTP 410 "API moved to
    # portal.esimcard.com", which a client checking only the response body would
    # read as a normal failure and retry forever.
    esimcard_base_url: str = "https://portal.esimcard.com/api/developer/reseller"
    esimcard_api_token: str = ""
    esimcard_timeout_seconds: int = 30

    # --- Cashback ------------------------------------------------------------
    # Repeat-purchase cashback: from the customer's second paid order onward, a
    # single-use code worth this percentage of the order is issued to them.
    #
    # Set to 0 to switch the scheme off. Kept as settings rather than hardcoded
    # because it is a marketing lever the business will want to move, and moving
    # it should not need a deploy of new logic — only a restart.
    # Referal komissiyasi: taklif qilingan odam BIRINCHI marta to'lov qilganda
    # taklif qilgan odamga tegadigan haq.
    #
    # Pog'onali, chunki kelishuv ham pog'onali: ko'proq mijoz olib kelgan agent
    # ko'proq oladi. Format -- `<nechanchi mijozdan>:<stavka>`, stavka `5%`
    # (buyurtma summasidan) yoki `5000` (qat'iy so'm) bo'lishi mumkin. Ikkalasi
    # ham qo'llab-quvvatlanadi, chunki kelishuv hali ikkala ko'rinishda ham
    # aytilgan; raqam o'zgarsa kod emas, shu satr o'zgaradi.
    referral_commission_tiers: str = "0:5%,100:6%,300:6.5%"
    # Kim referal bo'limini ko'radi. Bo'sh -- hamma ko'radi. Vergul bilan
    # ajratilgan e-pochtalar yozilsa, faqat o'shalar ko'radi: kelishuv
    # raqamlari hali qat'iy emas, va yakunlanmagan shartni butun mijozlar
    # bazasiga va'da qilib bo'lmaydi. Avval o'zimizda sinaladi, keyin ochiladi.
    referral_visible_to: str = ""
    # Eski, bir pog'onali sozlama. Faqat yuqoridagisi bo'sh qoldirilganda
    # ishlatiladi -- serverdagi .env hali eskicha bo'lsa, komissiya jimgina
    # nolga tushib qolmasin.
    referral_commission_uzs: int = 0

    loyalty_cashback_percent: int = 5
    # Which paid order first earns it. 2 means "every purchase after the first".
    loyalty_cashback_from_order: int = 2
    # How long the customer has to spend it. A reward with no expiry is a
    # liability that never ages off the books.
    loyalty_cashback_valid_days: int = 90

    cbu_currency_url: str = "https://cbu.uz/uz/arkhiv-kursov-valyut/json/"
    uzs_per_usd_fallback: float = 12000.0

    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    telegram_timeout_seconds: int = 10

    @field_validator("jwt_secret")
    @classmethod
    def _reject_placeholder_secret(cls, v: str) -> str:
        weak = {"dev-secret", "changeme", "secret", "replace-with-a-long-random-value"}
        if v.strip().lower() in weak:
            raise ValueError("JWT_SECRET is a placeholder value — generate a real secret")
        return v

    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @property
    def fulfillable_providers(self) -> tuple[str, ...]:
        return tuple(
            part.strip() for part in self.fulfillable_providers_raw.split(",") if part.strip()
        )

    @property
    def supplier_calls_enabled(self) -> bool:
        """Whether fulfilment may spend real money with a wholesaler."""
        return self.esim_provider != "mock"

    @property
    def sync_database_url(self) -> str:
        """psycopg URL for Celery workers that use the sync engine."""
        return str(self.database_url).replace("+asyncpg", "+psycopg")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
