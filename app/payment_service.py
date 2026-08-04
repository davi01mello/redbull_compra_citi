from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .db.models import OrderORM, PaymentORM
from .models import (
    Order,
    OrderCreate,
    PaymentProvider as PaymentProviderName,
    PaymentStatus,
    ReservationStatus,
)
from .payments import ChargeResult, PaymentProvider, PaymentProviderError
from .store import store


class PaymentServiceError(RuntimeError):
    pass


def payment_idempotency_key(order_id: UUID) -> str:
    return f"redbull-order-{order_id}"


def _provider_name(provider: PaymentProvider) -> PaymentProviderName:
    try:
        return PaymentProviderName(provider.provider_name.upper())
    except ValueError as exc:
        raise PaymentServiceError(
            f"Provedor não suportado: {provider.provider_name}"
        ) from exc


async def _load_order(db: AsyncSession, order_id: UUID) -> Order:
    order = await store.get_order(db, str(order_id))
    await db.commit()
    return order


async def _prepare_payment(
    db: AsyncSession,
    order_id: UUID,
    provider: PaymentProvider,
) -> tuple[Decimal, bool]:
    provider_name = _provider_name(provider)
    now = datetime.now(UTC)

    async with db.begin():
        order_result = await db.execute(
            select(OrderORM).where(OrderORM.id == order_id).with_for_update()
        )
        order = order_result.scalar_one_or_none()
        if order is None:
            raise PaymentServiceError("Pedido não encontrado")

        payment_result = await db.execute(
            select(PaymentORM)
            .where(PaymentORM.order_id == order_id)
            .with_for_update()
        )
        payment = payment_result.scalar_one_or_none()
        if payment is not None:
            if payment.provider != provider_name:
                raise PaymentServiceError(
                    "O pedido já está vinculado a outro provedor de pagamento"
                )
            if payment.status in {
                PaymentStatus.WAITING_PAYMENT,
                PaymentStatus.APPROVED,
            } and payment.copy_paste:
                return Decimal(payment.amount), True
            if payment.status in {
                PaymentStatus.FAILED,
                PaymentStatus.EXPIRED,
            }:
                raise PaymentServiceError(
                    "A cobrança deste pedido não pode ser gerada novamente"
                )
        else:
            payment = PaymentORM(
                id=uuid4(),
                order_id=order_id,
                provider=provider_name,
                status=PaymentStatus.CREATING,
                amount=order.total,
                currency="BRL",
                created_at=now,
                updated_at=now,
            )
            db.add(payment)

        if order.reservation_status != ReservationStatus.ACTIVE:
            raise PaymentServiceError("O pedido não possui uma reserva ativa")

        await db.flush()
        return Decimal(order.total), False


async def _save_charge(
    db: AsyncSession,
    order_id: UUID,
    charge: ChargeResult,
) -> None:
    now = datetime.now(UTC)
    async with db.begin():
        order_result = await db.execute(
            select(OrderORM).where(OrderORM.id == order_id).with_for_update()
        )
        order = order_result.scalar_one()
        payment_result = await db.execute(
            select(PaymentORM)
            .where(PaymentORM.order_id == order_id)
            .with_for_update()
        )
        payment = payment_result.scalar_one()

        if payment.status in {
            PaymentStatus.WAITING_PAYMENT,
            PaymentStatus.APPROVED,
        } and payment.copy_paste:
            return
        if payment.status != PaymentStatus.CREATING:
            raise PaymentServiceError("A cobrança não está mais em criação")
        if order.reservation_status != ReservationStatus.ACTIVE:
            raise PaymentServiceError("A reserva do pedido não está mais ativa")

        payment.provider_order_id = charge.provider_order_id
        payment.provider_payment_id = charge.provider_payment_id
        payment.status = PaymentStatus.WAITING_PAYMENT
        payment.amount = charge.amount
        payment.currency = "BRL"
        payment.copy_paste = charge.copy_paste
        payment.expires_at = charge.expires_at
        payment.updated_at = now

        order.txid = charge.provider_payment_id or charge.provider_order_id
        order.qr_code = charge.qr_code
        order.copy_paste = charge.copy_paste
        await db.flush()


async def ensure_order_payment(
    db: AsyncSession,
    order_id: UUID,
    provider: PaymentProvider,
) -> Order:
    amount, already_created = await _prepare_payment(db, order_id, provider)
    if already_created:
        return await _load_order(db, order_id)

    try:
        charge = await provider.create_charge(
            external_reference=str(order_id),
            amount=amount,
            idempotency_key=payment_idempotency_key(order_id),
        )
        if charge.amount != amount:
            raise PaymentProviderError(
                "O provedor retornou uma cobrança com valor diferente do pedido"
            )
        if not charge.copy_paste or not charge.qr_code:
            raise PaymentProviderError(
                "O provedor não retornou todos os dados necessários do PIX"
            )
    except PaymentProviderError as exc:
        await store.release_reservation(
            db,
            order_id,
            payment_status=PaymentStatus.FAILED,
        )
        raise PaymentServiceError(
            "Não foi possível criar a cobrança; a reserva foi liberada"
        ) from exc

    await _save_charge(db, order_id, charge)
    return await _load_order(db, order_id)


async def create_order_with_payment(
    db: AsyncSession,
    payload: OrderCreate,
    provider: PaymentProvider,
) -> Order:
    reserved_order = await store.reserve_order(db, payload)
    return await ensure_order_payment(db, reserved_order.id, provider)
