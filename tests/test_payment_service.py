import asyncio
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
    PaymentProvider,
    PaymentStatus,
    PricingAudience,
    ReservationStatus,
)
from app.payment_service import (
    PaymentServiceError,
    create_order_with_payment,
    ensure_order_payment,
    payment_idempotency_key,
)
from app.payments import ChargeResult, PaymentProviderError
from app.store import store


INITIAL_STOCK = {"original": 24, "tropical": 18, "melancia": 16}


async def reset_database() -> None:
    async with AsyncSessionLocal() as session, session.begin():
        await session.execute(delete(OrderORM))
        await session.execute(update(PriceTierORM).values(active=True))
        for product_id, stock in INITIAL_STOCK.items():
            await session.execute(
                update(ProductORM)
                .where(ProductORM.id == product_id)
                .values(stock=stock, reserved_stock=0)
            )
    await engine.dispose()


def order_payload() -> OrderCreate:
    return OrderCreate(
        buyer_name="Teste orquestração",
        pricing_audience=PricingAudience.MEMBER,
        items=[OrderItemInput(product_id="original", quantity=2)],
    )


class RecordingProvider:
    provider_name = "mock"

    def __init__(self, db=None, error: PaymentProviderError | None = None) -> None:
        self.db = db
        self.error = error
        self.calls: list[dict] = []

    async def create_charge(
        self,
        external_reference: str,
        amount: Decimal,
        idempotency_key: str,
    ) -> ChargeResult:
        self.calls.append(
            {
                "external_reference": external_reference,
                "amount": amount,
                "idempotency_key": idempotency_key,
                "transaction_open": self.db.in_transaction() if self.db else None,
            }
        )
        if self.error:
            raise self.error
        return ChargeResult(
            provider_order_id=f"provider-order-{external_reference}",
            provider_payment_id=f"provider-payment-{external_reference}",
            qr_code=f"https://example.test/pix/{external_reference}",
            copy_paste=f"PIX-{external_reference}",
            amount=amount,
            expires_at=datetime.now(UTC) + timedelta(minutes=30),
        )

    async def get_payment(self, _payment_id):
        raise NotImplementedError

    async def aclose(self) -> None:
        return None


def test_order_orchestration_happy_path_commits_reservation_before_provider():
    async def scenario():
        await reset_database()
        async with AsyncSessionLocal() as session:
            provider = RecordingProvider(db=session)
            order = await create_order_with_payment(
                session,
                order_payload(),
                provider,
            )

        async with AsyncSessionLocal() as session:
            payment = (
                await session.execute(
                    select(PaymentORM).where(PaymentORM.order_id == order.id)
                )
            ).scalar_one()
            product = (
                await session.execute(
                    select(ProductORM).where(ProductORM.id == "original")
                )
            ).scalar_one()
            persisted_order = (
                await session.execute(
                    select(OrderORM).where(OrderORM.id == order.id)
                )
            ).scalar_one()
            result = (provider.calls, payment, product, persisted_order, order)
        await engine.dispose()
        return result

    calls, payment, product, persisted_order, order = asyncio.run(scenario())
    assert len(calls) == 1
    assert calls[0]["transaction_open"] is False
    assert calls[0]["external_reference"] == str(order.id)
    assert calls[0]["idempotency_key"] == payment_idempotency_key(order.id)
    assert payment.provider == PaymentProvider.MOCK
    assert payment.status == PaymentStatus.WAITING_PAYMENT
    assert payment.provider_order_id == f"provider-order-{order.id}"
    assert payment.provider_payment_id == f"provider-payment-{order.id}"
    assert payment.copy_paste == f"PIX-{order.id}"
    assert payment.amount == Decimal("16.00")
    assert product.stock == INITIAL_STOCK["original"]
    assert product.reserved_stock == 2
    assert persisted_order.reservation_status == ReservationStatus.ACTIVE
    assert order.payment.copy_paste == f"PIX-{order.id}"
    assert order.payment.qr_code == f"https://example.test/pix/{order.id}"


@pytest.mark.parametrize(
    "provider_error",
    [
        PaymentProviderError("Mercado Pago recusou a cobrança"),
        PaymentProviderError("Tempo esgotado ao criar a cobrança"),
    ],
    ids=["http-error", "timeout"],
)
def test_provider_failure_releases_reservation_and_marks_payment_failed(
    provider_error,
):
    async def scenario():
        await reset_database()
        async with AsyncSessionLocal() as session:
            provider = RecordingProvider(db=session, error=provider_error)
            with pytest.raises(PaymentServiceError) as exc_info:
                await create_order_with_payment(session, order_payload(), provider)

        async with AsyncSessionLocal() as session:
            order = (await session.execute(select(OrderORM))).scalar_one()
            payment = (await session.execute(select(PaymentORM))).scalar_one()
            product = (
                await session.execute(
                    select(ProductORM).where(ProductORM.id == "original")
                )
            ).scalar_one()
            result = (
                str(exc_info.value),
                provider.calls,
                order.status,
                order.reservation_status,
                payment.status,
                product.stock,
                product.reserved_stock,
            )
        await engine.dispose()
        return result

    (
        message,
        calls,
        order_status,
        reservation_status,
        payment_status,
        stock,
        reserved_stock,
    ) = asyncio.run(scenario())
    assert "reserva foi liberada" in message
    assert calls[0]["transaction_open"] is False
    assert order_status == OrderStatus.PENDING
    assert reservation_status == ReservationStatus.RELEASED
    assert payment_status == PaymentStatus.FAILED
    assert stock == INITIAL_STOCK["original"]
    assert reserved_stock == 0


class SimulatedProcessCrash(BaseException):
    pass


class CrashOnceIdempotentProvider(RecordingProvider):
    def __init__(self, db) -> None:
        super().__init__(db=db)
        self.unique_charges: dict[str, ChargeResult] = {}

    async def create_charge(
        self,
        external_reference: str,
        amount: Decimal,
        idempotency_key: str,
    ) -> ChargeResult:
        charge = self.unique_charges.setdefault(
            idempotency_key,
            ChargeResult(
                provider_order_id=f"provider-order-{external_reference}",
                provider_payment_id=f"provider-payment-{external_reference}",
                qr_code=f"https://example.test/pix/{external_reference}",
                copy_paste=f"PIX-{external_reference}",
                amount=amount,
                expires_at=datetime.now(UTC) + timedelta(minutes=30),
            ),
        )
        self.calls.append(
            {
                "idempotency_key": idempotency_key,
                "transaction_open": self.db.in_transaction(),
            }
        )
        if len(self.calls) == 1:
            raise SimulatedProcessCrash
        return charge


def test_retry_same_order_reuses_stable_key_without_duplicate_charge():
    async def scenario():
        await reset_database()
        async with AsyncSessionLocal() as session:
            reserved_order = await store.reserve_order(session, order_payload())
            provider = CrashOnceIdempotentProvider(db=session)
            with pytest.raises(SimulatedProcessCrash):
                await ensure_order_payment(session, reserved_order.id, provider)

            retried_order = await ensure_order_payment(
                session,
                reserved_order.id,
                provider,
            )
            loaded_again = await ensure_order_payment(
                session,
                reserved_order.id,
                provider,
            )

        async with AsyncSessionLocal() as session:
            payments = list((await session.execute(select(PaymentORM))).scalars())
        await engine.dispose()
        return provider, reserved_order, retried_order, loaded_again, payments

    provider, reserved_order, retried_order, loaded_again, payments = asyncio.run(
        scenario()
    )
    expected_key = payment_idempotency_key(reserved_order.id)
    assert [call["idempotency_key"] for call in provider.calls] == [
        expected_key,
        expected_key,
    ]
    assert all(call["transaction_open"] is False for call in provider.calls)
    assert len(provider.unique_charges) == 1
    assert len(payments) == 1
    assert payments[0].status == PaymentStatus.WAITING_PAYMENT
    assert retried_order.id == loaded_again.id == reserved_order.id
