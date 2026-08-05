import asyncio
import hashlib
import hmac
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select, update

from app.config import Settings
from app.db.base import AsyncSessionLocal, engine
from app.db.models import (
    OrderORM,
    PaymentORM,
    PriceTierORM,
    ProductORM,
    WebhookEventORM,
)
from app.main import app
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
    process_mercadopago_payment,
)
from app.payments import ChargeResult, PaymentProviderError, PaymentResult
from app.store import store


INITIAL_STOCK = {"original": 24, "tropical": 18, "melancia": 16}
WEBHOOK_SECRET = "stage-7-webhook-secret"
APPROVED_AT = datetime(2026, 8, 4, 21, 30, tzinfo=UTC)


async def reset_database() -> None:
    async with AsyncSessionLocal() as session, session.begin():
        await session.execute(delete(WebhookEventORM))
        await session.execute(delete(OrderORM))
        await session.execute(update(PriceTierORM).values(active=True))
        for product_id, stock in INITIAL_STOCK.items():
            await session.execute(
                update(ProductORM)
                .where(ProductORM.id == product_id)
                .values(stock=stock, reserved_stock=0)
            )
    await engine.dispose()


class MercadoPagoWebhookTestProvider:
    provider_name = "mercadopago"

    def __init__(self) -> None:
        self.order_results: dict[str, PaymentResult] = {}
        self.order_ids_by_payment_id: dict[str, str] = {}
        self.queries: list[str] = []

    async def create_charge(
        self,
        external_reference: str,
        amount: Decimal,
        idempotency_key: str,
    ) -> ChargeResult:
        order_id = f"mp-order-{external_reference}"
        payment_id = f"mp-payment-{external_reference}"
        self.order_results[order_id] = PaymentResult(
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
            qr_code=f"https://example.test/pix/{external_reference}",
            copy_paste=f"PIX-{external_reference}",
            amount=amount,
            expires_at=datetime.now(UTC) + timedelta(minutes=30),
        )

    async def get_order(self, order_id: str) -> PaymentResult:
        self.queries.append(order_id)
        try:
            return self.order_results[order_id]
        except KeyError as exc:
            raise PaymentProviderError("Order de teste não encontrada") from exc

    def order_id_for_payment(self, payment_id: str) -> str:
        return self.order_ids_by_payment_id[payment_id]

    async def aclose(self) -> None:
        return None


def order_payload() -> OrderCreate:
    return OrderCreate(
        buyer_name="Webhook Mercado Pago",
        pricing_audience=PricingAudience.MEMBER,
        items=[OrderItemInput(product_id="original", quantity=2)],
    )


async def create_pending_order(provider: MercadoPagoWebhookTestProvider):
    await reset_database()
    async with AsyncSessionLocal() as session:
        order = await create_order_with_payment(session, order_payload(), provider)
    await engine.dispose()
    return order


def signature_headers(order_id: str, request_id: str = "request-stage-7") -> dict:
    timestamp = "1785879000"
    normalized_id = order_id.lower() if order_id.isalnum() else order_id
    manifest = f"id:{normalized_id};request-id:{request_id};ts:{timestamp};"
    digest = hmac.new(
        WEBHOOK_SECRET.encode(), manifest.encode(), hashlib.sha256
    ).hexdigest()
    return {
        "x-request-id": request_id,
        "x-signature": f"ts={timestamp},v1={digest}",
    }


def webhook_body(event_id: str, order_id: str) -> dict:
    return {
        "id": event_id,
        "type": "order",
        "action": "order.updated",
        "data": {"id": order_id},
    }


def webhook_settings() -> Settings:
    return Settings(
        mercadopago_access_token="test-access-token",
        mercadopago_webhook_secret=WEBHOOK_SECRET,
        payment_provider="mercadopago",
        charge_expires_in=1800,
    )


def post_webhook(provider, event_id, order_id, headers=None):
    with TestClient(app) as client:
        client.app.state.payment_provider = provider
        client.app.state.payment_settings = webhook_settings()
        return client.post(
            "/api/webhooks/mercadopago",
            json=webhook_body(event_id, order_id),
            headers=headers or signature_headers(order_id),
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
        event_count = (
            await session.execute(select(func.count()).select_from(WebhookEventORM))
        ).scalar_one()
        result = (
            order.status,
            order.reservation_status,
            order.paid_at,
            payment.status,
            payment.approved_at,
            product.stock,
            product.reserved_stock,
            event_count,
        )
    await engine.dispose()
    return result


async def load_event_references(event_id: str) -> tuple[str | None, str | None]:
    async with AsyncSessionLocal() as session:
        event = (
            await session.execute(
                select(WebhookEventORM).where(WebhookEventORM.event_id == event_id)
            )
        ).scalar_one()
        result = (event.provider_order_id, event.provider_payment_id)
    await engine.dispose()
    return result


def approve(provider, order_id):
    current = provider.order_results[order_id]
    provider.order_results[order_id] = PaymentResult(
        provider_order_id=order_id,
        provider_payment_id=current.provider_payment_id,
        status="APPROVED",
        amount=current.amount,
        currency="BRL",
        approved_at=APPROVED_AT,
    )


def test_valid_signature_and_approved_payment_marks_paid_and_consumes_stock():
    provider = MercadoPagoWebhookTestProvider()
    order = asyncio.run(create_pending_order(provider))
    order_id = provider.order_id_for_payment(order.payment.txid)
    approve(provider, order_id)

    response = post_webhook(provider, "event-approved", order_id)
    asyncio.run(engine.dispose())
    state = asyncio.run(load_state(order.id))

    assert response.status_code == 200
    assert response.json()["outcome"] == "approved"
    assert provider.queries == [order_id]
    assert asyncio.run(load_event_references("event-approved")) == (
        order_id,
        order.payment.txid,
    )
    assert state == (
        OrderStatus.PAID,
        ReservationStatus.CONSUMED,
        APPROVED_AT,
        PaymentStatus.APPROVED,
        APPROVED_AT,
        INITIAL_STOCK["original"] - 2,
        0,
        1,
    )


def test_invalid_signature_is_rejected_without_querying_or_changing_state():
    provider = MercadoPagoWebhookTestProvider()
    order = asyncio.run(create_pending_order(provider))
    order_id = provider.order_id_for_payment(order.payment.txid)

    response = post_webhook(
        provider,
        "event-invalid-signature",
        order_id,
        headers={"x-request-id": "request", "x-signature": "ts=1,v1=invalid"},
    )
    asyncio.run(engine.dispose())
    state = asyncio.run(load_state(order.id))

    assert response.status_code == 401
    assert provider.queries == []
    assert state[0] == OrderStatus.PENDING
    assert state[1] == ReservationStatus.ACTIVE
    assert state[3] == PaymentStatus.WAITING_PAYMENT
    assert state[5:] == (INITIAL_STOCK["original"], 2, 0)


def test_duplicate_webhook_does_not_decrement_stock_twice():
    provider = MercadoPagoWebhookTestProvider()
    order = asyncio.run(create_pending_order(provider))
    order_id = provider.order_id_for_payment(order.payment.txid)
    approve(provider, order_id)

    first = post_webhook(provider, "event-duplicate", order_id)
    asyncio.run(engine.dispose())
    second = post_webhook(provider, "event-duplicate", order_id)
    asyncio.run(engine.dispose())
    state = asyncio.run(load_state(order.id))

    assert first.json()["outcome"] == "approved"
    assert second.json()["outcome"] == "already_paid"
    assert state[5] == INITIAL_STOCK["original"] - 2
    assert state[6] == 0
    assert state[7] == 1


@pytest.mark.parametrize("status", ["PENDING", "REJECTED"])
def test_non_approved_payment_is_acknowledged_without_state_change(status):
    provider = MercadoPagoWebhookTestProvider()
    order = asyncio.run(create_pending_order(provider))
    order_id = provider.order_id_for_payment(order.payment.txid)
    current = provider.order_results[order_id]
    provider.order_results[order_id] = PaymentResult(
        provider_order_id=order_id,
        provider_payment_id=current.provider_payment_id,
        status=status,
        amount=current.amount,
        currency="BRL",
        approved_at=None,
    )

    response = post_webhook(provider, f"event-{status.lower()}", order_id)
    asyncio.run(engine.dispose())
    state = asyncio.run(load_state(order.id))

    assert response.status_code == 200
    assert response.json()["outcome"] == "not_approved"
    assert state[0] == OrderStatus.PENDING
    assert state[1] == ReservationStatus.ACTIVE
    assert state[3] == PaymentStatus.WAITING_PAYMENT
    assert state[5:] == (INITIAL_STOCK["original"], 2, 1)


def test_unknown_order_id_is_acknowledged_and_logged():
    asyncio.run(reset_database())
    provider = MercadoPagoWebhookTestProvider()
    order_id = "unknown-order-id"
    provider.order_results[order_id] = PaymentResult(
        provider_order_id=order_id,
        provider_payment_id="unknown-payment-id",
        status="APPROVED",
        amount=Decimal("8.50"),
        currency="BRL",
        approved_at=APPROVED_AT,
    )

    response = post_webhook(provider, "event-unknown", order_id)
    asyncio.run(engine.dispose())

    async def event_count():
        async with AsyncSessionLocal() as session:
            count = (
                await session.execute(
                    select(func.count()).select_from(WebhookEventORM)
                )
            ).scalar_one()
        await engine.dispose()
        return count

    assert response.status_code == 200
    assert response.json()["outcome"] == "unknown_order"
    assert asyncio.run(event_count()) == 1


def test_released_reservation_is_flagged_without_decrementing_stock():
    provider = MercadoPagoWebhookTestProvider()
    order = asyncio.run(create_pending_order(provider))
    order_id = provider.order_id_for_payment(order.payment.txid)

    async def release():
        async with AsyncSessionLocal() as session:
            released = await store.release_reservation(session, order.id)
            assert released is True
            async with session.begin():
                await session.execute(
                    update(PaymentORM)
                    .where(PaymentORM.order_id == order.id)
                    .values(status=PaymentStatus.FAILED)
                )
        await engine.dispose()

    asyncio.run(release())
    approve(provider, order_id)
    response = post_webhook(provider, "event-released", order_id)
    asyncio.run(engine.dispose())
    state = asyncio.run(load_state(order.id))

    assert response.status_code == 200
    assert response.json()["outcome"] == "reservation_not_active"
    assert state[0] == OrderStatus.PENDING
    assert state[1] == ReservationStatus.RELEASED
    assert state[3] == PaymentStatus.FAILED
    assert state[5:] == (INITIAL_STOCK["original"], 0, 1)


def test_two_concurrent_webhooks_decrement_stock_only_once():
    async def scenario():
        provider = MercadoPagoWebhookTestProvider()
        order = await create_pending_order(provider)
        order_id = provider.order_id_for_payment(order.payment.txid)
        approve(provider, order_id)
        provider_payment = provider.order_results[order_id]

        async def process(event_id):
            async with AsyncSessionLocal() as session:
                return await process_mercadopago_payment(
                    session,
                    event_id=event_id,
                    event_type="order.updated",
                    webhook_order_id=order_id,
                    provider_payment=provider_payment,
                )

        results = await asyncio.gather(
            process("event-concurrent-1"),
            process("event-concurrent-2"),
        )
        async with AsyncSessionLocal() as session:
            product = (
                await session.execute(
                    select(ProductORM).where(ProductORM.id == "original")
                )
            ).scalar_one()
            order_orm = (
                await session.execute(
                    select(OrderORM).where(OrderORM.id == order.id)
                )
            ).scalar_one()
            event_count = (
                await session.execute(
                    select(func.count()).select_from(WebhookEventORM)
                )
            ).scalar_one()
            state = (
                product.stock,
                product.reserved_stock,
                order_orm.status,
                order_orm.reservation_status,
                event_count,
            )
        await engine.dispose()
        return results, state

    results, state = asyncio.run(scenario())
    assert sorted(result.outcome for result in results) == ["already_paid", "approved"]
    assert state == (
        INITIAL_STOCK["original"] - 2,
        0,
        OrderStatus.PAID,
        ReservationStatus.CONSUMED,
        2,
    )
