from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import settings
from app.routers import account, auth, catalog, checkout, content, currency, support, webhooks

app = FastAPI(
    title="FastSIM API",
    description="eSIM commerce API — storefront, checkout and account services.",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    # Also allow any localhost/127.0.0.1 port so the Vite dev server works even
    # when it falls back to 5174, 5175, … (and when accessed without the proxy).
    allow_origin_regex=r"https?://(localhost|127\.0\.0\.1)(:\d+)?",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(catalog.router)
app.include_router(content.router)
app.include_router(checkout.router)
app.include_router(account.router)
app.include_router(currency.router)
app.include_router(support.router)
app.include_router(webhooks.router)


@app.get("/api/health", tags=["health"])
def health():
    return {"status": "ok", "service": "fastsim-api"}
