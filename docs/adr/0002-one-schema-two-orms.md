# ADR-0002 — One schema, two ORMs

**Status:** accepted (recording an existing decision), 2026-08-22
**Applies to:** `QulaySIM-admin` (owner) and `QulaySIM-backend` (mirror)

## Context

One PostgreSQL database is reached by two ORMs:

- **Django, in `QulaySIM-admin`** — owns every migration, and therefore the
  schema. It is also the back office: the operator edits catalogue, pricing and
  orders through it.
- **SQLAlchemy, in `QulaySIM-backend`** — maps the tables the storefront API
  reads and writes. It runs no migrations.

An architecture review flagged this: `Plan`, `Order` and `PromoCode` are each
declared twice, in two languages, and the second declaration is written by hand.
Two questions followed — is the duplication a mistake, and what does it actually
cost?

## Decision

**Keep Django as the schema owner and keep the SQLAlchemy mapping hand-written
and deliberately partial.**

Why not the alternatives:

- **Generate the mirror** (`sqlacodegen` against the database). The mapping is
  not just column names: it carries hybrid properties, the money conventions,
  and the comments that explain why a column exists — `Plan.coverage`,
  `Order.is_complimentary`, the 1/10000-USD supplier convention. Generated code
  loses all of it, and a generated file that then needs hand edits is the worst
  of both.
- **Move ownership to the API** (Alembic). The admin's migrations are also its
  forms, its list views and its permissions; splitting them from the schema puts
  every operator-facing change behind two repositories.
- **One shared ORM.** Django's async story and FastAPI's request model do not
  meet; this was settled before this ADR and is not being reopened.

## What the duplication actually costs, and what pays for it

The mirror drifting is **already guarded**: `tests/integration/test_schema_contract.py`
fails CI when a table or column the API maps stops existing, or when nullability
disagrees. That is the expensive failure and it is covered.

What was *not* covered is cheaper and more frequent: **nobody could look up what
a column is called.** On 2026-08-22 three production queries failed on guessed
names — `starting_price` (does not exist), `valid_from` (it is `valid_until`),
`esims_esim` (it is `orders_esim`). Each guess cost a round trip.

So the answer is documentation, not restructuring: `manage.py dump_schema` writes
`docs/schema.md` in the admin repo from the live tables, committed and
refreshed by CI.

## Consequences

- The mirror stays partial **on purpose**. A Django column the API does not need
  is not a gap, and the contract test deliberately checks one direction only.
- A migration that renames a column the API maps fails CI in the API repo, not
  the admin's. Whoever writes the migration has to look there.
- `docs/schema.md` is generated. Editing it by hand will be overwritten.
- Tables that exist only for the back office — `SellableShape`,
  `CatalogSyncRun`, `ComplimentaryGrant` — are never mirrored, and that is the
  normal case rather than an omission.
