import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select, update

from app.db.base import AsyncSessionLocal, engine
from app.db.models import (
    ManualReviewORM,
    OrderORM,
    PaymentORM,
    PriceTierORM,
    ProductORM,
    WebhookEventORM,
)
from app.main import app
from app.models import (
    ManualReviewReason,
    OrderCreate,
    OrderItemInput,
    OrderStatus,
    PaymentStatus,
    PricingAudience,
    ReservationStatus,
)
from app.payment_service import (
    PaymentCancellationConflict,
    cancel_order_payment,
    create_order_with_payment,
    expire_due_payments,
    process_mercadopago_payment,
)
from app.payments import ChargeResult, PaymentProviderError, PaymentResult
from app.store import store


INITIAL_STOCK = {"original": 24, "tropical": 18, "melancia": 16}
APPROVED_AT = datetime(2026, 8, 4, 22, 0, tzinfo=UTC)


class ExpirationTestProvider:
    provider_name = "mercadopago"

    def __init__(self) -> None:
        self.orders: dict[str, PaymentResult] = {}
        self.order_ids_by_payment_id: dict[str, str] = {}
        self.cancelled_order_ids: list[str] = []

    async def create_charge(
        self,
        external_reference: str,
        amount: Decimal,
        idempotency_key: str,
        payer_email: str,
    ) -> ChargeResult:
        assert payer_email == "comprador@example.com"
        order_id = f"expiration-order-{external_reference}"
        payment_id = f"expiration-payment-{external_reference}"
        self.orders[order_id] = PaymentResult(
            provider_order_id=order_id,
            provider_payment_id=payment_id,
            status="PENDING",
            amount=amount,
            currency="BRL",
            approved_at=None,
        )
        self.order_ids_by_payment_id[payment_id] = order_id
        return ChargeResult(
            provider_order_id=order_id,
            provider_payment_id=payment_id,
            qr_code=f"https://example.test/{external_reference}",
            copy_paste=f"PIX-{external_reference}",
            amount=amount,
            expires_at=datetime.now(UTC) + timedelta(minutes=30),
        )

    async def get_order(self, order_id: str) -> PaymentResult:
        return self.orders[order_id]

    async def cancel_order(self, order_id: str, idempotency_key: str) -> None:
        assert idempotency_key.endswith("-cancel")
        self.cancelled_order_ids.append(order_id)
        current = self.orders[order_id]
        self.orders[order_id] = PaymentResult(
            provider_order_id=current.provider_order_id,
            provider_payment_id=current.provider_payment_id,
            status="CANCELLED",
            amount=current.amount,
            currency=current.currency,
            approved_at=None,
        )

    async def aclose(self) -> None:
        return None

    def approve(self, payment_id: str, amount: Decimal | None = None) -> PaymentResult:
        order_id = self.order_ids_by_payment_id[payment_id]
        current = self.orders[order_id]
        approved = PaymentResult(
            provider_order_id=order_id,
            provider_payment_id=payment_id,
            status="APPROVED",
            amount=amount if amount is not None else current.amount,
            currency="BRL",
            approved_at=APPROVED_AT,
        )
        self.orders[order_id] = approved
        return approved


class ApprovalDuringCancellationProvider(ExpirationTestProvider):
    async def cancel_order(self, order_id: str, idempotency_key: str) -> None:
        raise PaymentProviderError("cannot_cancel_order: processed")


async def reset_database(stock_overrides: dict[str, int] | None = None) -> None:
    stocks = INITIAL_STOCK | (stock_overrides or {})
    async with AsyncSessionLocal() as session, session.begin():
        await session.execute(delete(ManualReviewORM))
        await session.execute(delete(WebhookEventORM))
        await session.execute(delete(OrderORM))
        await session.execute(update(PriceTierORM).values(active=True))
        for product_id, stock in stocks.items():
            await session.execute(
                update(ProductORM)
                .where(ProductORM.id == product_id)
                .values(stock=stock, reserved_stock=0)
            )
    await engine.dispose()


def payload(name: str = "Expiração", quantity: int = 1) -> OrderCreate:
    return OrderCreate(
        buyer_name=name,
        payer_email="comprador@example.com",
        pricing_audience=PricingAudience.MEMBER,
        items=[OrderItemInput(product_id="original", quantity=quantity)],
    )


async def create_order(provider: ExpirationTestProvider, name: str = "Expiração"):
    async with AsyncSessionLocal() as session:
        return await create_order_with_payment(session, payload(name), provider)


async def make_due(order_id, due_at: datetime) -> None:
    async with AsyncSessionLocal() as session, session.begin():
        await session.execute(
            update(PaymentORM)
            .where(PaymentORM.order_id == order_id)
            .values(expires_at=due_at - timedelta(seconds=1))
        )
    await engine.dispose()


async def process_approved(provider, order, event_id: str):
    payment_id = order.payment.txid
    provider_payment = provider.approve(payment_id)
    async with AsyncSessionLocal() as session:
        return await process_mercadopago_payment(
            session,
            event_id=event_id,
            event_type="order.updated",
            webhook_order_id=provider_payment.provider_order_id,
            provider_payment=provider_payment,
        )


async def load_state(order_id):
    async with AsyncSessionLocal() as session:
        order = (
            await session.execute(select(OrderORM).where(OrderORM.id == order_id))
        ).scalar_one()
        payment = (
            await session.execute(
                select(PaymentORM).where(PaymentORM.order_id == order_id)
            )
        ).scalar_one()
        product = (
            await session.execute(
                select(ProductORM).where(ProductORM.id == "original")
            )
        ).scalar_one()
        reviews = list(
            (
                await session.execute(
                    select(ManualReviewORM)
                    .where(ManualReviewORM.order_id == order_id)
                    .order_by(ManualReviewORM.reason)
                )
            ).scalars()
        )
        result = {
            "order_status": order.status,
            "reservation_status": order.reservation_status,
            "paid_at": order.paid_at,
            "payment_status": payment.status,
            "approved_at": payment.approved_at,
            "stock": product.stock,
            "reserved_stock": product.reserved_stock,
            "review_reasons": [review.reason for review in reviews],
        }
    await engine.dispose()
    return result


def test_due_payment_expires_and_releases_reservation_without_cancelling_order():
    async def scenario():
        await reset_database()
        provider = ExpirationTestProvider()
        order = await create_order(provider)
        cutoff = datetime.now(UTC)
        await make_due(order.id, cutoff)
        async with AsyncSessionLocal() as session:
            expired = await expire_due_payments(session, cutoff)
        state = await load_state(order.id)
        return expired, state

    expired, state = asyncio.run(scenario())
    assert expired == 1
    assert state == {
        "order_status": OrderStatus.PENDING,
        "reservation_status": ReservationStatus.RELEASED,
        "paid_at": None,
        "payment_status": PaymentStatus.EXPIRED,
        "approved_at": None,
        "stock": INITIAL_STOCK["original"],
        "reserved_stock": 0,
        "review_reasons": [],
    }


def test_due_payment_is_cancelled_at_provider_before_local_expiration():
    async def scenario():
        await reset_database()
        provider = ExpirationTestProvider()
        order = await create_order(provider)
        cutoff = datetime.now(UTC)
        await make_due(order.id, cutoff)
        async with AsyncSessionLocal() as session:
            expired = await expire_due_payments(
                session,
                cutoff,
                provider=provider,
            )
        return expired, provider.cancelled_order_ids, await load_state(order.id)

    expired, cancelled_order_ids, state = asyncio.run(scenario())
    assert expired == 1
    assert len(cancelled_order_ids) == 1
    assert state["payment_status"] == PaymentStatus.EXPIRED
    assert state["reservation_status"] == ReservationStatus.RELEASED


def test_payment_confirmed_after_buyer_cancellation_is_reconciled_and_flagged():
    async def scenario():
        await reset_database()
        provider = ExpirationTestProvider()
        order = await create_order(provider)
        async with AsyncSessionLocal() as session:
            cancelled = await cancel_order_payment(session, order.id, provider)
        result = await process_approved(
            provider,
            cancelled,
            "event-after-buyer-cancellation",
        )
        return result, await load_state(order.id)

    result, state = asyncio.run(scenario())
    assert result.outcome == "approved_after_cancellation"
    assert state["order_status"] == OrderStatus.PAID
    assert state["payment_status"] == PaymentStatus.APPROVED
    assert state["reservation_status"] == ReservationStatus.CONSUMED
    assert state["stock"] == INITIAL_STOCK["original"] - 1
    assert state["review_reasons"] == [
        ManualReviewReason.PAYMENT_AFTER_CANCELLATION.value
    ]


def test_approval_racing_with_cancellation_is_reconciled_as_paid():
    async def scenario():
        await reset_database()
        provider = ApprovalDuringCancellationProvider()
        order = await create_order(provider)
        provider.approve(order.payment.txid)
        async with AsyncSessionLocal() as session:
            with pytest.raises(PaymentCancellationConflict):
                await cancel_order_payment(session, order.id, provider)
        return await load_state(order.id)

    state = asyncio.run(scenario())
    assert state["order_status"] == OrderStatus.PAID
    assert state["payment_status"] == PaymentStatus.APPROVED
    assert state["reservation_status"] == ReservationStatus.CONSUMED
    assert state["stock"] == INITIAL_STOCK["original"] - 1
    assert state["reserved_stock"] == 0


def test_expiration_is_idempotent():
    async def scenario():
        await reset_database()
        provider = ExpirationTestProvider()
        order = await create_order(provider)
        cutoff = datetime.now(UTC)
        await make_due(order.id, cutoff)
        async with AsyncSessionLocal() as first_session:
            first = await expire_due_payments(first_session, cutoff)
        async with AsyncSessionLocal() as second_session:
            second = await expire_due_payments(second_session, cutoff)
        return first, second, await load_state(order.id)

    first, second, state = asyncio.run(scenario())
    assert (first, second) == (1, 0)
    assert state["reserved_stock"] == 0
    assert state["payment_status"] == PaymentStatus.EXPIRED


def test_expiration_and_webhook_race_keep_one_consistent_final_state():
    async def scenario():
        await reset_database()
        provider = ExpirationTestProvider()
        order = await create_order(provider)
        cutoff = datetime.now(UTC)
        await make_due(order.id, cutoff)
        provider_payment = provider.approve(order.payment.txid)

        async def expire():
            async with AsyncSessionLocal() as session:
                return await expire_due_payments(session, cutoff)

        async def webhook():
            async with AsyncSessionLocal() as session:
                return await process_mercadopago_payment(
                    session,
                    event_id="expiration-race",
                    event_type="order.updated",
                    webhook_order_id=provider_payment.provider_order_id,
                    provider_payment=provider_payment,
                )

        expired, webhook_result = await asyncio.gather(expire(), webhook())
        return expired, webhook_result, await load_state(order.id)

    expired, webhook_result, state = asyncio.run(scenario())
    assert expired in {0, 1}
    assert webhook_result.outcome in {"approved", "approved_late"}
    assert state["order_status"] == OrderStatus.PAID
    assert state["reservation_status"] == ReservationStatus.CONSUMED
    assert state["payment_status"] == PaymentStatus.APPROVED
    assert state["stock"] == INITIAL_STOCK["original"] - 1
    assert state["reserved_stock"] == 0


def test_late_webhook_after_expiration_is_accepted_and_consumes_available_stock():
    async def scenario():
        await reset_database()
        provider = ExpirationTestProvider()
        order = await create_order(provider)
        cutoff = datetime.now(UTC)
        await make_due(order.id, cutoff)
        async with AsyncSessionLocal() as session:
            assert await expire_due_payments(session, cutoff) == 1
        result = await process_approved(provider, order, "late-payment")
        return result, await load_state(order.id)

    result, state = asyncio.run(scenario())
    assert result.outcome == "approved_late"
    assert result.processed is True
    assert state["order_status"] == OrderStatus.PAID
    assert state["reservation_status"] == ReservationStatus.CONSUMED
    assert state["payment_status"] == PaymentStatus.APPROVED
    assert state["paid_at"] == APPROVED_AT
    assert state["stock"] == INITIAL_STOCK["original"] - 1
    assert state["review_reasons"] == []


def test_late_webhook_with_reallocated_unit_is_confirmed_and_flagged():
    async def scenario():
        await reset_database({"original": 1})
        provider = ExpirationTestProvider()
        late_order = await create_order(provider, "Pagamento tardio")
        cutoff = datetime.now(UTC)
        await make_due(late_order.id, cutoff)
        async with AsyncSessionLocal() as session:
            assert await expire_due_payments(session, cutoff) == 1

        replacement_order = await create_order(provider, "Realocação")
        replacement_result = await process_approved(
            provider, replacement_order, "replacement-paid"
        )
        late_result = await process_approved(provider, late_order, "late-conflict")
        return replacement_result, late_result, await load_state(late_order.id)

    replacement_result, late_result, state = asyncio.run(scenario())
    assert replacement_result.outcome == "approved"
    assert late_result.outcome == "approved_late_stock_conflict"
    assert late_result.processed is True
    assert state["order_status"] == OrderStatus.PAID
    assert state["payment_status"] == PaymentStatus.APPROVED
    assert state["paid_at"] == APPROVED_AT
    assert state["stock"] == 0
    assert state["reserved_stock"] == 0
    assert state["review_reasons"] == [
        ManualReviewReason.LATE_PAYMENT_STOCK_CONFLICT.value
    ]


def test_reservation_released_after_charge_failure_is_still_rejected_and_flagged():
    async def scenario():
        await reset_database()
        provider = ExpirationTestProvider()
        order = await create_order(provider)
        async with AsyncSessionLocal() as session:
            assert await store.release_reservation(session, order.id) is True
            async with session.begin():
                await session.execute(
                    update(PaymentORM)
                    .where(PaymentORM.order_id == order.id)
                    .values(status=PaymentStatus.FAILED)
                )
        result = await process_approved(provider, order, "failed-charge-anomaly")
        return result, await load_state(order.id)

    result, state = asyncio.run(scenario())
    assert result.outcome == "reservation_not_active"
    assert result.processed is False
    assert state["order_status"] == OrderStatus.PENDING
    assert state["payment_status"] == PaymentStatus.FAILED
    assert state["stock"] == INITIAL_STOCK["original"]
    assert state["review_reasons"] == [
        ManualReviewReason.RESERVATION_NOT_ACTIVE.value
    ]


@pytest.mark.parametrize(
    ("approved_at", "amount", "expected_outcome", "expected_reason"),
    [
        (
            None,
            Decimal("8.50"),
            "missing_approved_at",
            ManualReviewReason.MISSING_APPROVED_AT,
        ),
        (
            APPROVED_AT,
            Decimal("9.50"),
            "amount_mismatch",
            ManualReviewReason.AMOUNT_MISMATCH,
        ),
    ],
)
def test_webhook_anomalies_are_listed_for_manual_review(
    approved_at,
    amount,
    expected_outcome,
    expected_reason,
):
    async def scenario():
        await reset_database()
        provider = ExpirationTestProvider()
        order = await create_order(provider, "Revisão manual")
        provider_payment = PaymentResult(
            provider_order_id=provider.order_ids_by_payment_id[order.payment.txid],
            provider_payment_id=order.payment.txid,
            status="APPROVED",
            amount=amount,
            currency="BRL",
            approved_at=approved_at,
        )
        async with AsyncSessionLocal() as session:
            result = await process_mercadopago_payment(
                session,
                event_id=f"review-{expected_outcome}",
                event_type="order.updated",
                webhook_order_id=provider_payment.provider_order_id,
                provider_payment=provider_payment,
            )
        return order, result

    order, result = asyncio.run(scenario())
    asyncio.run(engine.dispose())
    with TestClient(app) as client:
        response = client.get("/api/manual-reviews")
    asyncio.run(engine.dispose())

    assert result.outcome == expected_outcome
    assert response.status_code == 404


def test_mock_charge_lost_after_restart_still_releases_reservation():
    """O mock guarda cobranças em memória; após reiniciar o servidor a
    reserva vencida precisa ser liberada em vez de ficar presa."""
    from app.config import settings
    from app.payments import MockPaymentProvider

    async def scenario():
        await reset_database()
        order = await create_order(MockPaymentProvider(settings))
        cutoff = datetime.now(UTC)
        await make_due(order.id, cutoff)
        restarted = MockPaymentProvider(settings)  # memória perdida
        async with AsyncSessionLocal() as session:
            expired = await expire_due_payments(session, cutoff, provider=restarted)
        state = await load_state(order.id)
        await engine.dispose()
        return expired, state

    expired, state = asyncio.run(scenario())
    assert expired == 1
    assert state["payment_status"] == PaymentStatus.EXPIRED
    assert state["reservation_status"] == ReservationStatus.RELEASED
    assert state["reserved_stock"] == 0


def test_inconsistent_reservation_does_not_block_other_expirations():
    async def scenario():
        await reset_database()
        provider = ExpirationTestProvider()
        broken = await create_order(provider, "Inconsistente")  # produto "original"
        async with AsyncSessionLocal() as session:
            healthy = await create_order_with_payment(
                session,
                OrderCreate(
                    buyer_name="Normal",
                    payer_email="comprador@example.com",
                    pricing_audience=PricingAudience.MEMBER,
                    items=[OrderItemInput(product_id="tropical", quantity=1)],
                ),
                provider,
            )
        cutoff = datetime.now(UTC)
        # O inconsistente vence antes e, portanto, é processado primeiro.
        await make_due(broken.id, cutoff - timedelta(seconds=30))
        await make_due(healthy.id, cutoff)
        async with AsyncSessionLocal() as session, session.begin():
            await session.execute(
                update(ProductORM)
                .where(ProductORM.id == "original")
                .values(reserved_stock=0)  # menor que o exigido pelo pedido
            )
        async with AsyncSessionLocal() as session:
            first = await expire_due_payments(session, cutoff, provider=provider)
        async with AsyncSessionLocal() as session:
            second = await expire_due_payments(session, cutoff, provider=provider)
        async with AsyncSessionLocal() as session:
            reviews = (
                await session.execute(
                    select(ManualReviewORM.order_id, ManualReviewORM.reason)
                )
            ).all()
            healthy_order = (
                await session.execute(
                    select(OrderORM).where(OrderORM.id == healthy.id)
                )
            ).scalar_one()
            broken_order = (
                await session.execute(
                    select(OrderORM).where(OrderORM.id == broken.id)
                )
            ).scalar_one()
        await engine.dispose()
        return broken.id, first, second, reviews, healthy_order, broken_order

    broken_id, first, second, reviews, healthy_order, broken_order = asyncio.run(
        scenario()
    )
    # O pedido normal é liberado mesmo com o inconsistente falhando antes dele.
    assert first == 1
    assert healthy_order.reservation_status == ReservationStatus.RELEASED
    # O inconsistente fica intacto e vai para revisão manual, sem se repetir.
    assert broken_order.reservation_status == ReservationStatus.ACTIVE
    assert [(r[0], r[1]) for r in reviews] == [
        (broken_id, "RESERVATION_RELEASE_FAILED")
    ]
    assert second == 0
