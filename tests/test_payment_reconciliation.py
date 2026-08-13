import asyncio
from argparse import Namespace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import delete, select, update

from app.db.base import AsyncSessionLocal, engine
from app.db.models import OrderORM, PaymentORM, PriceTierORM, ProductORM
from app.models import (
    OrderCreate,
    OrderItemInput,
    OrderStatus,
    PaymentStatus,
    PricingAudience,
    ReservationStatus,
)
from app.payment_service import (
    create_order_with_payment,
    expire_due_payments,
    process_mercadopago_payment,
    reconcile_pending_mercadopago_payments,
)
from app.payments import ChargeResult, PaymentProviderError, PaymentResult
from scripts import reconcile_payment as reconcile_command


INITIAL_STOCK = 24
APPROVED_AT = datetime(2026, 8, 11, 12, 0, tzinfo=UTC)


class ReconciliationProvider:
    provider_name = "mercadopago"

    def __init__(self) -> None:
        self.orders: dict[str, PaymentResult] = {}
        self.errors: set[str] = set()
        self.queries: list[str] = []
        self.cancelled: list[str] = []

    async def create_charge(
        self,
        external_reference: str,
        amount: Decimal,
        idempotency_key: str,
        payer_email: str,
    ) -> ChargeResult:
        assert idempotency_key
        assert payer_email == "comprador@example.com"
        provider_order_id = f"reconcile-order-{external_reference}"
        provider_payment_id = f"reconcile-payment-{external_reference}"
        self.orders[provider_order_id] = PaymentResult(
            provider_order_id=provider_order_id,
            provider_payment_id=provider_payment_id,
            status="WAITING_PAYMENT",
            amount=amount,
            currency="BRL",
            approved_at=None,
        )
        return ChargeResult(
            provider_order_id=provider_order_id,
            provider_payment_id=provider_payment_id,
            qr_code=f"https://example.test/{external_reference}",
            copy_paste=f"PIX-{external_reference}",
            amount=amount,
            expires_at=datetime.now(UTC) + timedelta(minutes=30),
        )

    async def get_order(self, order_id: str) -> PaymentResult:
        self.queries.append(order_id)
        if order_id in self.errors:
            raise PaymentProviderError("Falha de comunicação simulada")
        return self.orders[order_id]

    async def cancel_order(self, order_id: str, idempotency_key: str) -> None:
        if order_id in self.errors:
            raise PaymentProviderError("Falha de comunicação simulada")
        self.cancelled.append(order_id)

    async def aclose(self) -> None:
        return None

    def approve(self, provider_order_id: str) -> PaymentResult:
        current = self.orders[provider_order_id]
        approved = PaymentResult(
            provider_order_id=provider_order_id,
            provider_payment_id=current.provider_payment_id,
            status="APPROVED",
            amount=current.amount,
            currency="BRL",
            approved_at=APPROVED_AT,
        )
        self.orders[provider_order_id] = approved
        return approved


async def reset_database() -> None:
    async with AsyncSessionLocal() as db, db.begin():
        await db.execute(delete(OrderORM))
        await db.execute(update(PriceTierORM).values(active=True))
        await db.execute(
            update(ProductORM)
            .where(ProductORM.id == "original")
            .values(stock=INITIAL_STOCK, reserved_stock=0)
        )
    await engine.dispose()


async def create_pending(provider: ReconciliationProvider, name: str):
    async with AsyncSessionLocal() as db:
        order = await create_order_with_payment(
            db,
            OrderCreate(
                buyer_name=name,
                payer_email="comprador@example.com",
                pricing_audience=PricingAudience.MEMBER,
                items=[OrderItemInput(product_id="original", quantity=1)],
            ),
            provider,
        )
    provider_order_id = next(
        key for key in provider.orders if str(order.id) in key
    )
    return order, provider_order_id


async def load_state(order_id):
    async with AsyncSessionLocal() as db:
        order = (
            await db.execute(select(OrderORM).where(OrderORM.id == order_id))
        ).scalar_one()
        payment = (
            await db.execute(
                select(PaymentORM).where(PaymentORM.order_id == order_id)
            )
        ).scalar_one()
        product = (
            await db.execute(
                select(ProductORM).where(ProductORM.id == "original")
            )
        ).scalar_one()
        result = (
            order.status,
            order.reservation_status,
            payment.status,
            product.stock,
            product.reserved_stock,
        )
    await engine.dispose()
    return result


def test_reconciliation_approves_pending_payment_and_is_idempotent():
    async def scenario():
        await reset_database()
        provider = ReconciliationProvider()
        order, provider_order_id = await create_pending(provider, "Reconciliação")
        provider.approve(provider_order_id)
        async with AsyncSessionLocal() as first_db:
            first = await reconcile_pending_mercadopago_payments(first_db, provider)
        async with AsyncSessionLocal() as second_db:
            second = await reconcile_pending_mercadopago_payments(second_db, provider)
        state = await load_state(order.id)
        await engine.dispose()
        return first, second, state

    first, second, state = asyncio.run(scenario())
    assert first.approved == 1
    assert second.considered == 0
    assert state == (
        OrderStatus.PAID,
        ReservationStatus.CONSUMED,
        PaymentStatus.APPROVED,
        INITIAL_STOCK - 1,
        0,
    )


def test_pending_remote_payment_is_not_changed():
    async def scenario():
        await reset_database()
        provider = ReconciliationProvider()
        order, _ = await create_pending(provider, "Ainda pendente")
        async with AsyncSessionLocal() as db:
            summary = await reconcile_pending_mercadopago_payments(db, provider)
        return summary, await load_state(order.id)

    summary, state = asyncio.run(scenario())
    assert summary.pending == 1
    assert state == (
        OrderStatus.PENDING,
        ReservationStatus.ACTIVE,
        PaymentStatus.WAITING_PAYMENT,
        INITIAL_STOCK,
        1,
    )


@pytest.mark.parametrize(
    ("remote_status", "order_status", "payment_status"),
    [
        ("CANCELLED", OrderStatus.CANCELLED, PaymentStatus.CANCELLED),
        ("EXPIRED", OrderStatus.PENDING, PaymentStatus.EXPIRED),
    ],
)
def test_remote_terminal_state_releases_reservation_safely(
    remote_status, order_status, payment_status
):
    async def scenario():
        await reset_database()
        provider = ReconciliationProvider()
        order, provider_order_id = await create_pending(provider, "Terminal remoto")
        current = provider.orders[provider_order_id]
        provider.orders[provider_order_id] = PaymentResult(
            provider_order_id=provider_order_id,
            provider_payment_id=current.provider_payment_id,
            status=remote_status,
            amount=current.amount,
            currency="BRL",
            approved_at=None,
        )
        async with AsyncSessionLocal() as db:
            summary = await reconcile_pending_mercadopago_payments(db, provider)
        return summary, await load_state(order.id)

    summary, state = asyncio.run(scenario())
    assert summary.terminal == 1
    assert state == (
        order_status,
        ReservationStatus.RELEASED,
        payment_status,
        INITIAL_STOCK,
        0,
    )


def test_one_communication_failure_does_not_stop_batch():
    async def scenario():
        await reset_database()
        provider = ReconciliationProvider()
        failed_order, failed_id = await create_pending(provider, "Falha isolada")
        approved_order, approved_id = await create_pending(provider, "Segue lote")
        provider.errors.add(failed_id)
        provider.approve(approved_id)
        async with AsyncSessionLocal() as db:
            summary = await reconcile_pending_mercadopago_payments(db, provider)
        return (
            summary,
            await load_state(failed_order.id),
            await load_state(approved_order.id),
        )

    summary, failed_state, approved_state = asyncio.run(scenario())
    assert summary.failed == 1
    assert summary.approved == 1
    assert failed_state[0:3] == (
        OrderStatus.PENDING,
        ReservationStatus.ACTIVE,
        PaymentStatus.WAITING_PAYMENT,
    )
    assert approved_state[0:3] == (
        OrderStatus.PAID,
        ReservationStatus.CONSUMED,
        PaymentStatus.APPROVED,
    )


def test_reconciliation_and_webhook_decrement_stock_only_once():
    async def scenario():
        await reset_database()
        provider = ReconciliationProvider()
        order, provider_order_id = await create_pending(provider, "Concorrência")
        approved = provider.approve(provider_order_id)

        async def reconcile():
            async with AsyncSessionLocal() as db:
                return await reconcile_pending_mercadopago_payments(db, provider)

        async def webhook():
            async with AsyncSessionLocal() as db:
                return await process_mercadopago_payment(
                    db,
                    event_id="concurrent-webhook",
                    event_type="order.updated",
                    webhook_order_id=provider_order_id,
                    provider_payment=approved,
                )

        await asyncio.gather(reconcile(), webhook())
        return await load_state(order.id)

    state = asyncio.run(scenario())
    assert state[0:3] == (
        OrderStatus.PAID,
        ReservationStatus.CONSUMED,
        PaymentStatus.APPROVED,
    )
    assert state[3:] == (INITIAL_STOCK - 1, 0)


def test_reconciliation_happens_before_expiration_and_prevents_cancel():
    async def scenario():
        await reset_database()
        provider = ReconciliationProvider()
        order, provider_order_id = await create_pending(provider, "No vencimento")
        provider.approve(provider_order_id)
        cutoff = datetime.now(UTC)
        async with AsyncSessionLocal() as db, db.begin():
            await db.execute(
                update(PaymentORM)
                .where(PaymentORM.order_id == order.id)
                .values(expires_at=cutoff - timedelta(seconds=1))
            )
        async with AsyncSessionLocal() as db:
            expired = await expire_due_payments(
                db, as_of=cutoff, provider=provider
            )
        return expired, provider.cancelled, await load_state(order.id)

    expired, cancelled, state = asyncio.run(scenario())
    assert expired == 0
    assert cancelled == []
    assert state[0:3] == (
        OrderStatus.PAID,
        ReservationStatus.CONSUMED,
        PaymentStatus.APPROVED,
    )


def test_missing_provider_order_id_is_ignored_safely():
    async def scenario():
        await reset_database()
        provider = ReconciliationProvider()
        order, _ = await create_pending(provider, "Sem ID remoto")
        cutoff = datetime.now(UTC)
        async with AsyncSessionLocal() as db, db.begin():
            await db.execute(
                update(PaymentORM)
                .where(PaymentORM.order_id == order.id)
                .values(
                    provider_order_id=None,
                    expires_at=cutoff - timedelta(seconds=1),
                )
            )
        async with AsyncSessionLocal() as db:
            summary = await reconcile_pending_mercadopago_payments(db, provider)
        async with AsyncSessionLocal() as db:
            expired = await expire_due_payments(
                db, as_of=cutoff, provider=provider
            )
        return summary, expired, await load_state(order.id)

    summary, expired, state = asyncio.run(scenario())
    assert summary.considered == 0
    assert expired == 0
    assert state[0:3] == (
        OrderStatus.PENDING,
        ReservationStatus.ACTIVE,
        PaymentStatus.WAITING_PAYMENT,
    )


def test_communication_failure_does_not_expire_or_release_reservation():
    async def scenario():
        await reset_database()
        provider = ReconciliationProvider()
        order, provider_order_id = await create_pending(provider, "Falha externa")
        provider.errors.add(provider_order_id)
        cutoff = datetime.now(UTC)
        async with AsyncSessionLocal() as db, db.begin():
            await db.execute(
                update(PaymentORM)
                .where(PaymentORM.order_id == order.id)
                .values(expires_at=cutoff - timedelta(seconds=1))
            )
        async with AsyncSessionLocal() as db:
            expired = await expire_due_payments(
                db, as_of=cutoff, provider=provider
            )
        return expired, await load_state(order.id)

    expired, state = asyncio.run(scenario())
    assert expired == 0
    assert state == (
        OrderStatus.PENDING,
        ReservationStatus.ACTIVE,
        PaymentStatus.WAITING_PAYMENT,
        INITIAL_STOCK,
        1,
    )


def test_manual_command_dry_run_then_real_reuses_central_transaction(
    monkeypatch, capsys
):
    async def scenario():
        await reset_database()
        provider = ReconciliationProvider()
        order, provider_order_id = await create_pending(provider, "Reparo manual")
        provider.approve(provider_order_id)
        monkeypatch.setattr(
            reconcile_command,
            "settings",
            type("CommandSettings", (), {"payment_provider": "mercadopago"})(),
        )
        monkeypatch.setattr(
            reconcile_command,
            "create_payment_provider",
            lambda _settings: provider,
        )
        dry_args = Namespace(
            order_id=order.id,
            provider_order_id=None,
            dry_run=True,
        )
        assert await reconcile_command.run(dry_args) == 0
        dry_state = await load_state(order.id)
        real_args = Namespace(
            order_id=None,
            provider_order_id=provider_order_id,
            dry_run=False,
        )
        assert await reconcile_command.run(real_args) == 0
        real_state = await load_state(order.id)
        return dry_state, real_state

    dry_state, real_state = asyncio.run(scenario())
    output = capsys.readouterr().out
    assert "dry_run=true" in output
    assert "outcome=approved" in output
    assert dry_state[0:3] == (
        OrderStatus.PENDING,
        ReservationStatus.ACTIVE,
        PaymentStatus.WAITING_PAYMENT,
    )
    assert real_state == (
        OrderStatus.PAID,
        ReservationStatus.CONSUMED,
        PaymentStatus.APPROVED,
        INITIAL_STOCK - 1,
        0,
    )
