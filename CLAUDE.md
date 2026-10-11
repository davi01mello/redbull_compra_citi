# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Red Bull PDV: a mobile self-checkout for selling Red Bull, backed by FastAPI + PostgreSQL, with real Pix charges via Mercado Pago (and a `mock` provider for local development). Comments, docstrings, and log messages in this codebase are written in Portuguese — keep that convention when editing existing files.

## Commands

```bash
# Setup
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
docker compose up -d postgres
alembic upgrade head

# Run the app (http://127.0.0.1:8000, docs at /docs)
uvicorn app.main:app --reload

# Tests (uses TEST_DATABASE_URL only, never DATABASE_URL — see below)
docker compose up -d postgres_test
pytest
pytest tests/test_payment_service.py            # single file
pytest tests/test_payment_service.py::test_name  # single test

# Migrations
alembic current
alembic heads
alembic upgrade head
alembic downgrade -1

# Manual reconciliation for a stuck order (dry-run first, always)
python -m scripts.reconcile_payment --order-id UUID_DO_PEDIDO --dry-run
python -m scripts.reconcile_payment --provider-order-id ID_DA_ORDER --dry-run
```

There is no lint/format command configured in this repo (no ruff/black/mypy config present).

## Architecture

### Request flow

`app/main.py` wires FastAPI routes to two layers:
- `app/store.py` (`DatabaseStore`) — plain CRUD/read paths: listing products, price tiers, fetching an order.
- `app/payment_service.py` — all state-changing payment/order logic. This is where the real business rules live (reservation, charge creation, webhook processing, expiration, reconciliation, cancellation). Route handlers are thin; put new business logic here, not in `main.py`.

`app/payments.py` defines the `PaymentProvider` protocol and two implementations: `MockPaymentProvider` (dev-only, used when `PAYMENT_PROVIDER=mock`) and `MercadoPagoProvider` (real Pix charges via Mercado Pago's Orders API). `create_payment_provider(settings)` selects the implementation at startup and stores it on `app.state.payment_provider` (see the `lifespan` context manager in `main.py`). The mock provider's `/api/mock/*` routes return 404 whenever the Mercado Pago provider is active.

`app/models.py` holds the Pydantic API schemas and shared enums (`OrderStatus`, `PaymentStatus`, `ReservationStatus`, etc.); `app/db/models.py` holds the SQLAlchemy ORM models. Keep these in sync manually — they are separate layers by design (API contract vs. persistence).

### Payment/order state machine (the core of this codebase)

Every order carries three parallel states that must stay consistent inside single transactions:
- `Order.status`: `PENDING` → `PAID` / `CANCELLED`
- `Payment.status`: `CREATING` → `WAITING_PAYMENT` → `APPROVED` / `EXPIRED` / `CANCELLED` / `FAILED`
- `Order.reservation_status` (stock hold): `ACTIVE` → `CONSUMED` (on payment) / `RELEASED` (on cancel/expire)

All transitions in `payment_service.py` follow the same pattern: `SELECT ... FOR UPDATE` on `OrderORM`/`PaymentORM` (and `ProductORM` when touching stock) inside `db.begin()`, re-check the current state (never assume it hasn't changed since the caller last read it), then mutate. This defends against concurrent webhook deliveries, expiration sweeps, and manual reconciliation racing each other.

Key entry points and what they do:
- `create_order_with_payment` — reserves stock (`store.reserve_order`) then creates the charge (`ensure_order_payment`).
- `process_mercadopago_payment` — the single choke point for confirming a payment, called from the webhook handler, from `cancel_order_payment` (when a cancel races an approval), from periodic reconciliation, and from the expiration sweep. It is idempotent: recorded via `WebhookEventORM` (unique on `(provider, event_id)`), and it re-derives outcome from current DB + provider state rather than trusting caller intent. It handles the "late approval" case (payment approved after the reservation was already released) by re-checking unreserved stock and flagging a `ManualReview` when there's a conflict, instead of silently overselling.
- `expire_due_payments` — runs on a background asyncio loop (`_expiration_loop` in `main.py`, interval = `PAYMENT_EXPIRATION_CHECK_INTERVAL`). Before expiring anything it reconciles pending Mercado Pago payments first, so a late webhook/slow poll doesn't get incorrectly expired.
- `cancel_order_payment` — cancels at the provider, then locally; if provider cancellation fails, it re-queries the provider and runs it back through `process_mercadopago_payment` in case the payment was actually approved in the meantime.
- Ambiguous states (amount mismatch, missing `approved_at`, stock conflict on a late approval, reservation not active, payment after cancellation) are never silently resolved — they're written to `manual_reviews` via `_flag_manual_review` for a human to check. See `ManualReviewReason` in `app/models.py`.

**Never fix `orders.status` or decrement stock directly via SQL.** Use `scripts/reconcile_payment.py`, which reuses the same transactional `process_mercadopago_payment` function as the webhook. Read-only diagnostic queries live in `docs/payment_diagnostics.sql`.

### Payer email

The payer's email is never an environment variable (don't add `MERCADO_PAYER_EMAIL` / `MERCADOPAGO_PAYER_EMAIL`). Each buyer enters their own email at checkout; it's validated client- and server-side, normalized (`normalize_payer_email` in `app/models.py`), and persisted to `orders.payer_email`. Older rows may have `NULL` here. Charge creation uses exclusively the email stored on the order (`payer.email`), never a config value.

### Webhook security

`POST /api/webhooks/mercadopago` validates `x-signature`/`x-request-id` against `MERCADOPAGO_WEBHOOK_SECRET` (`verify_mercadopago_signature` in `app/payments.py`) using the `data.id` query param as the payload for the signature — not the request body. The body is optional and only used for extra event metadata. Never log tokens, signatures, QR codes, copy-paste Pix strings, or full emails.

### Database / Alembic

This service's ORM does **not** own the whole database: `users` and `stock_adjustments` are external tables that must be preserved, along with legacy columns `products.description`, `products.color`, `products.price` that exist in some installs but not the institutional DB. These are excluded from autogenerate via `UNMANAGED_TABLES` / `include_object` in `app/db/migrations/env.py` — do not remove that filter, and do not let autogenerate propose dropping them.

Before touching migrations in a real deployment, always run, in order: `alembic current`, `alembic heads`, confirm a backup exists, test upgrade+downgrade against a copy/test DB, verify `users`/`stock_adjustments` are untouched, then apply. Never run `alembic stamp head`, and never apply a migration against the institutional DB without explicit authorization. Full history/rationale for past migration surprises is in `docs/alteracoes-recentes.md`.

### Tests

`tests/conftest.py` enforces that tests use `TEST_DATABASE_URL` exclusively — it's validated to use the `postgresql+asyncpg` driver, to have a database name starting with `test` or ending in `_test`, and to differ from `DATABASE_URL`; the harness refuses to run otherwise. It force-runs `alembic upgrade head` against that database as a session-scoped autouse fixture, and forces `PAYMENT_PROVIDER=mock` for the test process before any app module is imported.

### Frontend

`app/static/` is a vanilla HTML/CSS/JS mobile checkout UI (`index.html`, `app.js`, `styles.css`) served directly by FastAPI's `StaticFiles`/`FileResponse` — no build step, no framework.
