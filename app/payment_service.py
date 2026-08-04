from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from .db.models import (
    OrderItemORM,
    OrderORM,
    PaymentORM,
    ProductORM,
    WebhookEventORM,
)
from .models import (
    Order,
    OrderCreate,
    OrderStatus,
    PaymentProvider as PaymentProviderName,
    PaymentStatus,
    ReservationStatus,
)
from .payments import (
    ChargeResult,
    PaymentProvider,
    PaymentProviderError,
    PaymentResult,
)
from .store import store


class PaymentServiceError(RuntimeError):
    pass


@dataclass(frozen=True)
class WebhookProcessingResult:
    outcome: str
    order_id: UUID | None = None

    @property
    def processed(self) -> bool:
        return self.outcome == "approved"


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


async def _record_webhook_event(
    db: AsyncSession,
    event_id: str,
    event_type: str,
    provider_payment_id: str,
) -> None:
    statement = (
        insert(WebhookEventORM)
        .values(
            provider=PaymentProviderName.MERCADOPAGO,
            event_id=event_id,
            event_type=event_type,
            provider_payment_id=provider_payment_id,
            processed_at=datetime.now(UTC),
        )
        .on_conflict_do_nothing(index_elements=["provider", "event_id"])
    )
    await db.execute(statement)


async def process_mercadopago_payment(
    db: AsyncSession,
    event_id: str,
    event_type: str,
    webhook_payment_id: str,
    provider_payment: PaymentResult,
) -> WebhookProcessingResult:
    async with db.begin():
        if provider_payment.provider_payment_id != webhook_payment_id:
            await _record_webhook_event(
                db, event_id, event_type, webhook_payment_id
            )
            return WebhookProcessingResult("provider_id_mismatch")

        payment_reference = (
            await db.execute(
                select(PaymentORM.id, PaymentORM.order_id)
                .where(
                    PaymentORM.provider
                    == PaymentProviderName.MERCADOPAGO,
                    PaymentORM.provider_payment_id == webhook_payment_id,
                )
            )
        ).one_or_none()
        if payment_reference is None:
            await _record_webhook_event(
                db, event_id, event_type, webhook_payment_id
            )
            return WebhookProcessingResult("unknown_payment")

        payment_id, order_id = payment_reference
        if provider_payment.status.upper() != "APPROVED":
            await _record_webhook_event(
                db, event_id, event_type, webhook_payment_id
            )
            return WebhookProcessingResult("not_approved", order_id)
        if provider_payment.approved_at is None:
            await _record_webhook_event(
                db, event_id, event_type, webhook_payment_id
            )
            return WebhookProcessingResult("missing_approved_at", order_id)

        order = (
            await db.execute(
                select(OrderORM).where(OrderORM.id == order_id).with_for_update()
            )
        ).scalar_one()
        payment = (
            await db.execute(
                select(PaymentORM)
                .where(PaymentORM.id == payment_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one()

        if order.status == OrderStatus.PAID:
            await _record_webhook_event(
                db, event_id, event_type, webhook_payment_id
            )
            return WebhookProcessingResult("already_paid", order_id)
        if payment.status == PaymentStatus.APPROVED:
            await _record_webhook_event(
                db, event_id, event_type, webhook_payment_id
            )
            return WebhookProcessingResult("inconsistent_payment_state", order_id)
        if order.reservation_status != ReservationStatus.ACTIVE:
            await _record_webhook_event(
                db, event_id, event_type, webhook_payment_id
            )
            return WebhookProcessingResult("reservation_not_active", order_id)
        if Decimal(payment.amount) != provider_payment.amount:
            await _record_webhook_event(
                db, event_id, event_type, webhook_payment_id
            )
            return WebhookProcessingResult("amount_mismatch", order_id)

        items = list(
            (
                await db.execute(
                    select(OrderItemORM)
                    .where(OrderItemORM.order_id == order_id)
                    .order_by(OrderItemORM.product_id)
                )
            ).scalars()
        )
        quantities: dict[str, int] = {}
        for item in items:
            quantities[item.product_id] = (
                quantities.get(item.product_id, 0) + item.quantity
            )

        product_ids = sorted(quantities)
        products = {
            product.id: product
            for product in (
                await db.execute(
                    select(ProductORM)
                    .where(ProductORM.id.in_(product_ids))
                    .order_by(ProductORM.id)
                    .with_for_update()
                )
            ).scalars()
        }
        for product_id in product_ids:
            product = products.get(product_id)
            quantity = quantities[product_id]
            if (
                product is None
                or product.reserved_stock < quantity
                or product.stock < quantity
            ):
                await _record_webhook_event(
                    db, event_id, event_type, webhook_payment_id
                )
                return WebhookProcessingResult("inconsistent_stock", order_id)

        for product_id in product_ids:
            quantity = quantities[product_id]
            products[product_id].reserved_stock -= quantity
            products[product_id].stock -= quantity

        payment.status = PaymentStatus.APPROVED
        payment.approved_at = provider_payment.approved_at
        payment.updated_at = provider_payment.approved_at
        order.status = OrderStatus.PAID
        order.paid_at = provider_payment.approved_at
        order.reservation_status = ReservationStatus.CONSUMED
        await _record_webhook_event(db, event_id, event_type, webhook_payment_id)
        await db.flush()

        return WebhookProcessingResult("approved", order_id)
