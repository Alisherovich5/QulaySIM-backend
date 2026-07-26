# QulaySIM API

Async FastAPI service behind the QulaySIM storefront. It is one of three
repositories that make up the platform:

```
┌──────────────────┐   HTTPS    ┌──────────────────┐
│  QulaySIM        │ ─────────▶ │  QulaySIM-backend│  ← this repo
│  React + Vite    │   /api/*   │  FastAPI (async) │
└──────────────────┘            └────────┬─────────┘
                                         │ SQLAlchemy 2.0 / asyncpg
                                         ▼
                                ┌──────────────────┐
                                │   PostgreSQL     │
                                └────────┬─────────┘
                                         ▲ Django ORM / psycopg
                                ┌────────┴─────────┐
                                │ QulaySIM-admin   │  ← SCHEMA OWNER
                                │ Django + Unfold  │
                                └──────────────────┘
```

**Django owns the schema.** Every migration lives in `QulaySIM-admin`; this
service maps onto the tables those migrations create. There is no Alembic here
on purpose. `tests/integration/test_schema_contract.py` fails the build if the
two drift apart — that test is the contract between the repositories.

Redis backs the cache, the rate limiter and the Celery broker. Celery runs the
work that must not happen inside a request: supplier provisioning, eSIM
expiry, and cache warming.

## Layout

```
app/
├── core/            config, logging, security, errors, cache, rate limiting, middleware
├── db/              declarative base, async session, ORM models (one module per domain)
├── domain/          pure business rules — no I/O, no SQLAlchemy, fully unit-testable
├── repositories/    data access; every query lives here
├── services/        use cases that orchestrate repositories, domain and cache
├── integrations/    outbound HTTP: eSIM Access, Telegram, CBU exchange rate
├── api/v1/routers/  HTTP surface only — no business logic
├── schemas/         Pydantic request/response models
└── workers/         Celery app, tasks and the sync session they use
tests/
├── unit/            domain rules; no database, no Redis
└── integration/     real Postgres + Redis, including the schema contract
```

The layering rule: **routers → services → repositories → database.** A router
never writes a query; a domain function never touches I/O. That is what makes
the money rules testable without infrastructure.

## Running it

```bash
cp .env.example .env         # fill in JWT_SECRET and DJANGO_SECRET_KEY
docker compose up -d         # the whole platform
docker compose exec api python -m scripts.seed    # demo catalogue
```

| Service    | URL                     | Notes                                   |
|------------|-------------------------|-----------------------------------------|
| storefront | http://localhost:8080   | nginx; proxies `/api` to the API         |
| API        | http://localhost:8001   | also reachable same-origin via `:8080`   |
| admin      | http://localhost:8002   | Django, owns the schema                  |

Postgres and Redis are not published to the host — nothing outside the stack
needs them. The `migrate` service runs Django's migrations and every other
service waits for it, so the API never starts against a half-built schema.

Serving the storefront and the API from one origin means the refresh cookie is
first-party and no CORS preflight is involved.

Locally:

```bash
python3.12 -m venv .venv && .venv/bin/pip install -e ".[dev]"
cp .env.example .env         # then fill in JWT_SECRET
python -m scripts.seed       # demo catalogue
uvicorn app.main:app --reload
celery -A app.workers.celery_app.celery_app worker --loglevel=INFO
celery -A app.workers.celery_app.celery_app beat   --loglevel=INFO
```

Apply the Django migrations first — this service will not create tables:

```bash
cd ../QulaySIM-admin && python manage.py migrate
```

## Configuration

`JWT_SECRET`, `DATABASE_URL` and `REDIS_URL` are **required**. There is no
fall-back default for any secret: a missing value crashes the process at import
rather than quietly booting an insecure service. `JWT_SECRET` must be at least
32 characters and known placeholder strings are rejected outright.

See `.env.example` for the full list.

## Tests

```bash
pytest                          # needs Postgres + Redis
pytest tests/unit               # no infrastructure required
ruff check app tests
ruff format --check app tests
mypy app                        # strict
```

CI runs all four plus a Docker build, and applies the Django migrations from
`QulaySIM-admin` before the integration suite so the schema contract is checked
against the real thing.

## Operational notes

- `/api/health` is liveness (no dependencies). `/api/health/ready` checks
  Postgres and Redis; only a database outage marks the instance unready,
  because the cache and rate limiter both degrade gracefully.
- Every response carries `X-Request-ID`; supply your own to trace a request
  end to end. Logs are JSON in production, human-readable elsewhere.
- Errors share one envelope: `{code, detail, request_id}`. `detail` is kept at
  the top level because the storefront reads `error.response.data.detail`.
- Rate limits are Redis-backed and therefore shared across workers. They fail
  **open** — losing Redis must not lock customers out of logging in.
- Refresh tokens are single-use. Presenting a spent one is treated as reuse:
  it is rejected and logged.
