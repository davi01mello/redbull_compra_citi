from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
import logging
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from .db.models import (
    ManualReviewORM,
    OrderItemORM,
    OrderORM,
    PaymentORM,
    ProductORM,
    WebhookEventORM,
)
from .models import (
    ManualReview,
    ManualReviewReason,
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


logger = logging.getLogger(__name__)


class PaymentServiceError(RuntimeError):
    pass


class PaymentCancellationConflict(PaymentServiceError):
    pass


@dataclass(frozen=True)
class WebhookProcessingResult:
    outcome: str
    order_id: UUID | None = None

    @property
    def processed(self) -> bool:
        return self.outcome in {
            "approved",
            "approved_late",
            "approved_late_stock_conflict",
            "approved_after_cancellation",
        }


def payment_idempotency_key(order_id: UUID) -> str:
    return f"redbull-order-{order_id}"


def cancellation_idempotency_key(order_id: UUID) -> str:
    return f"redbull-order-{order_id}-cancel"


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
                PaymentStatus.CANCELLED,
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
        payment.qr_code = charge.qr_code
        payment.expires_at = charge.expires_at
        payment.updated_at = now
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
        logger.info(
            "Cobrança criada: order_id=%s provider_order_id=%s status=%s",
            order_id,
            charge.provider_order_id,
            charge.status,
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
        logger.error(
            "Falha ao criar cobrança no Mercado Pago: order_id=%s error=%s",
            order_id,
            exc,
        )
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


async def cancel_order_payment(
    db: AsyncSession,
    order_id: UUID,
    provider: PaymentProvider,
) -> Order:
    provider_name = _provider_name(provider)
    already_cancelled = False
    provider_order_id: str | None = None

    async with db.begin():
        order = (
            await db.execute(
                select(OrderORM).where(OrderORM.id == order_id).with_for_update()
            )
        ).scalar_one_or_none()
        payment = (
            await db.execute(
                select(PaymentORM)
                .where(PaymentORM.order_id == order_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if order is None or payment is None:
            raise PaymentCancellationConflict("Pedido não encontrado")
        if payment.provider != provider_name:
            raise PaymentCancellationConflict(
                "O pedido está vinculado a outro provedor de pagamento"
            )
        if (
            order.status == OrderStatus.CANCELLED
            and payment.status == PaymentStatus.CANCELLED
        ):
            already_cancelled = True
        elif order.status == OrderStatus.PAID or payment.status == PaymentStatus.APPROVED:
            raise PaymentCancellationConflict(
                "O pagamento já foi confirmado e não pode ser cancelado"
            )
        elif payment.status not in {
            PaymentStatus.CREATING,
            PaymentStatus.WAITING_PAYMENT,
        }:
            raise PaymentCancellationConflict(
                "Esta cobrança não está mais disponível para cancelamento"
            )
        provider_order_id = payment.provider_order_id

    if already_cancelled:
        return await _load_order(db, order_id)
    if not provider_order_id:
        raise PaymentCancellationConflict(
            "A cobrança ainda não possui um identificador no provedor"
        )

    try:
        await provider.cancel_order(
            provider_order_id,
            cancellation_idempotency_key(order_id),
        )
    except PaymentProviderError as exc:
        try:
            provider_payment = await provider.get_order(provider_order_id)
        except PaymentProviderError:
            provider_payment = None
        if provider_payment is not None and provider_payment.status.upper() == "APPROVED":
            reconciliation = await process_mercadopago_payment(
                db,
                event_id=f"cancel-reconciliation-{provider_order_id}",
                event_type="cancel.reconciliation",
                webhook_order_id=provider_order_id,
                provider_payment=provider_payment,
            )
            if reconciliation.processed or reconciliation.outcome == "already_paid":
                raise PaymentCancellationConflict(
                    "O pagamento foi aprovado antes do cancelamento e já foi confirmado."
                ) from exc
        raise PaymentServiceError(
            "Não foi possível interromper a cobrança. Tente novamente."
        ) from exc

    now = datetime.now(UTC)
    async with db.begin():
        order = (
            await db.execute(
                select(OrderORM).where(OrderORM.id == order_id).with_for_update()
            )
        ).scalar_one()
        payment = (
            await db.execute(
                select(PaymentORM)
                .where(PaymentORM.order_id == order_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one()

        if (
            order.status == OrderStatus.CANCELLED
            and payment.status == PaymentStatus.CANCELLED
        ):
            pass
        elif order.status == OrderStatus.PAID or payment.status == PaymentStatus.APPROVED:
            raise PaymentCancellationConflict(
                "O pagamento foi confirmado enquanto o cancelamento era processado"
            )
        elif payment.status not in {
            PaymentStatus.CREATING,
            PaymentStatus.WAITING_PAYMENT,
        }:
            raise PaymentCancellationConflict(
                "A cobrança mudou de estado enquanto era cancelada"
            )
        else:
            if order.reservation_status == ReservationStatus.ACTIVE:
                quantities = await _load_quantities(db, order_id)
                product_ids = sorted(quantities)
                products = await _lock_products(db, product_ids)
                for product_id in product_ids:
                    product = products.get(product_id)
                    quantity = quantities[product_id]
                    if product is None or product.reserved_stock < quantity:
                        raise RuntimeError(
                            f"Reserva inconsistente para o produto '{product_id}'"
                        )
                for product_id in product_ids:
                    products[product_id].reserved_stock -= quantities[product_id]
                order.reservation_status = ReservationStatus.RELEASED

            order.status = OrderStatus.CANCELLED
            payment.status = PaymentStatus.CANCELLED
            payment.updated_at = now
            await db.flush()

    return await _load_order(db, order_id)


async def _record_webhook_event(
    db: AsyncSession,
    event_id: str,
    event_type: str,
    provider_order_id: str,
    provider_payment_id: str | None,
) -> None:
    statement = (
        insert(WebhookEventORM)
        .values(
            provider=PaymentProviderName.MERCADOPAGO,
            event_id=event_id,
            event_type=event_type,
            provider_order_id=provider_order_id,
            provider_payment_id=provider_payment_id,
            processed_at=datetime.now(UTC),
        )
        .on_conflict_do_nothing(index_elements=["provider", "event_id"])
    )
    await db.execute(statement)


async def _flag_manual_review(
    db: AsyncSession,
    order_id: UUID,
    reason: ManualReviewReason,
    details: str | None = None,
) -> None:
    statement = (
        insert(ManualReviewORM)
        .values(
            id=uuid4(),
            order_id=order_id,
            reason=reason.value,
            details=details,
            created_at=datetime.now(UTC),
        )
        .on_conflict_do_nothing(index_elements=["order_id", "reason"])
    )
    await db.execute(statement)


async def list_manual_reviews(
    db: AsyncSession,
    unresolved_only: bool = True,
) -> list[ManualReview]:
    statement = (
        select(ManualReviewORM, OrderORM.buyer_name)
        .join(OrderORM, OrderORM.id == ManualReviewORM.order_id)
        .order_by(ManualReviewORM.created_at)
    )
    if unresolved_only:
        statement = statement.where(ManualReviewORM.resolved_at.is_(None))
    rows = (await db.execute(statement)).all()
    return [
        ManualReview(
            id=review.id,
            order_id=review.order_id,
            buyer_name=buyer_name,
            reason=ManualReviewReason(review.reason),
            details=review.details,
            created_at=review.created_at,
            resolved_at=review.resolved_at,
        )
        for review, buyer_name in rows
    ]


async def _load_quantities(
    db: AsyncSession,
    order_id: UUID,
) -> dict[str, int]:
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
        quantities[item.product_id] = quantities.get(item.product_id, 0) + item.quantity
    return quantities


async def _lock_products(
    db: AsyncSession,
    product_ids: list[str],
) -> dict[str, ProductORM]:
    return {
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


async def expire_due_payments(
    db: AsyncSession,
    as_of: datetime | None = None,
    provider: PaymentProvider | None = None,
) -> int:
    cutoff = as_of or datetime.now(UTC)
    async with db.begin():
        candidate_payments = list(
            (
                await db.execute(
                    select(
                        PaymentORM.order_id,
                        PaymentORM.provider,
                        PaymentORM.provider_order_id,
                    )
                    .where(
                        PaymentORM.status.in_(
                            {
                                PaymentStatus.CREATING,
                                PaymentStatus.WAITING_PAYMENT,
                            }
                        ),
                        PaymentORM.expires_at.is_not(None),
                        PaymentORM.expires_at <= cutoff,
                    )
                    .order_by(PaymentORM.expires_at, PaymentORM.order_id)
                )
            ).all()
        )

    candidate_order_ids: list[UUID] = []
    configured_provider = _provider_name(provider) if provider is not None else None
    for order_id, payment_provider, provider_order_id in candidate_payments:
        if provider is not None:
            if payment_provider != configured_provider:
                logger.warning(
                    "Cobrança vencida usa provedor diferente do configurado: "
                    "order_id=%s provider=%s",
                    order_id,
                    payment_provider.value,
                )
                continue
            if provider_order_id:
                try:
                    await provider.cancel_order(
                        provider_order_id,
                        cancellation_idempotency_key(order_id),
                    )
                except PaymentProviderError as exc:
                    logger.warning(
                        "Não foi possível cancelar cobrança vencida no provedor: "
                        "order_id=%s provider_order_id=%s error=%s",
                        order_id,
                        provider_order_id,
                        exc,
                    )
                    continue
        candidate_order_ids.append(order_id)

    expired = 0
    for order_id in candidate_order_ids:
        async with db.begin():
            order = (
                await db.execute(
                    select(OrderORM)
                    .where(OrderORM.id == order_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            payment = (
                await db.execute(
                    select(PaymentORM)
                    .where(PaymentORM.order_id == order_id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            ).scalar_one_or_none()
            if (
                order is None
                or payment is None
                or order.status == OrderStatus.PAID
                or payment.status
                not in {PaymentStatus.CREATING, PaymentStatus.WAITING_PAYMENT}
                or payment.expires_at is None
                or payment.expires_at > cutoff
            ):
                continue

            if order.reservation_status == ReservationStatus.ACTIVE:
                quantities = await _load_quantities(db, order_id)
                product_ids = sorted(quantities)
                products = await _lock_products(db, product_ids)
                for product_id in product_ids:
                    product = products.get(product_id)
                    quantity = quantities[product_id]
                    if product is None or product.reserved_stock < quantity:
                        raise RuntimeError(
                            f"Reserva inconsistente para o produto '{product_id}'"
                        )
                for product_id in product_ids:
                    products[product_id].reserved_stock -= quantities[product_id]
                order.reservation_status = ReservationStatus.RELEASED

            payment.status = PaymentStatus.EXPIRED
            payment.updated_at = cutoff
            await db.flush()
            expired += 1

    return expired


async def process_mercadopago_payment(
    db: AsyncSession,
    event_id: str,
    event_type: str,
    webhook_order_id: str,
    provider_payment: PaymentResult,
) -> WebhookProcessingResult:
    async with db.begin():
        if provider_payment.provider_order_id != webhook_order_id:
            await _record_webhook_event(
                db,
                event_id,
                event_type,
                webhook_order_id,
                provider_payment.provider_payment_id,
            )
            return WebhookProcessingResult("provider_id_mismatch")

        payment_reference = (
            await db.execute(
                select(PaymentORM.id, PaymentORM.order_id)
                .where(
                    PaymentORM.provider
                    == PaymentProviderName.MERCADOPAGO,
                    PaymentORM.provider_order_id == webhook_order_id,
                )
            )
        ).one_or_none()
        if payment_reference is None:
            await _record_webhook_event(
                db,
                event_id,
                event_type,
                webhook_order_id,
                provider_payment.provider_payment_id,
            )
            return WebhookProcessingResult("unknown_order")

        payment_id, order_id = payment_reference
        if provider_payment.status.upper() != "APPROVED":
            await _record_webhook_event(
                db,
                event_id,
                event_type,
                webhook_order_id,
                provider_payment.provider_payment_id,
            )
            return WebhookProcessingResult("not_approved", order_id)
        if provider_payment.approved_at is None:
            await _flag_manual_review(
                db,
                order_id,
                ManualReviewReason.MISSING_APPROVED_AT,
                "O provedor informou pagamento aprovado sem date_approved.",
            )
            await _record_webhook_event(
                db,
                event_id,
                event_type,
                webhook_order_id,
                provider_payment.provider_payment_id,
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
                db,
                event_id,
                event_type,
                webhook_order_id,
                provider_payment.provider_payment_id,
            )
            return WebhookProcessingResult("already_paid", order_id)
        if payment.status == PaymentStatus.APPROVED:
            await _record_webhook_event(
                db,
                event_id,
                event_type,
                webhook_order_id,
                provider_payment.provider_payment_id,
            )
            return WebhookProcessingResult("inconsistent_payment_state", order_id)
        if Decimal(payment.amount) != provider_payment.amount:
            await _flag_manual_review(
                db,
                order_id,
                ManualReviewReason.AMOUNT_MISMATCH,
                f"Esperado {payment.amount} {payment.currency}; recebido "
                f"{provider_payment.amount} {provider_payment.currency}.",
            )
            await _record_webhook_event(
                db,
                event_id,
                event_type,
                webhook_order_id,
                provider_payment.provider_payment_id,
            )
            return WebhookProcessingResult("amount_mismatch", order_id)

        late_after_release = (
            order.reservation_status == ReservationStatus.RELEASED
            and payment.status in {
                PaymentStatus.EXPIRED,
                PaymentStatus.CANCELLED,
            }
        )
        if (
            order.reservation_status != ReservationStatus.ACTIVE
            and not late_after_release
        ):
            await _flag_manual_review(
                db,
                order_id,
                ManualReviewReason.RESERVATION_NOT_ACTIVE,
                f"Reserva {order.reservation_status.value} com pagamento local "
                f"{payment.status.value}.",
            )
            await _record_webhook_event(
                db,
                event_id,
                event_type,
                webhook_order_id,
                provider_payment.provider_payment_id,
            )
            return WebhookProcessingResult("reservation_not_active", order_id)

        quantities = await _load_quantities(db, order_id)
        product_ids = sorted(quantities)
        products = await _lock_products(db, product_ids)
        outcome = "approved"
        if late_after_release:
            cancelled_by_buyer = payment.status == PaymentStatus.CANCELLED
            if cancelled_by_buyer:
                await _flag_manual_review(
                    db,
                    order_id,
                    ManualReviewReason.PAYMENT_AFTER_CANCELLATION,
                    "O provedor confirmou um pagamento após o cancelamento do comprador.",
                )
            conflicts: list[str] = []
            for product_id in product_ids:
                product = products.get(product_id)
                quantity = quantities[product_id]
                if (
                    product is None
                    or product.stock - product.reserved_stock < quantity
                ):
                    conflicts.append(product_id)
                else:
                    product.stock -= quantity
            if conflicts:
                await _flag_manual_review(
                    db,
                    order_id,
                    ManualReviewReason.LATE_PAYMENT_STOCK_CONFLICT,
                    "Sem estoque não reservado suficiente para: "
                    + ", ".join(conflicts),
                )
                outcome = "approved_late_stock_conflict"
            elif cancelled_by_buyer:
                outcome = "approved_after_cancellation"
            else:
                outcome = "approved_late"
        else:
            for product_id in product_ids:
                product = products.get(product_id)
                quantity = quantities[product_id]
                if (
                    product is None
                    or product.reserved_stock < quantity
                    or product.stock < quantity
                ):
                    await _record_webhook_event(
                        db,
                        event_id,
                        event_type,
                        webhook_order_id,
                        provider_payment.provider_payment_id,
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
        await _record_webhook_event(
            db,
            event_id,
            event_type,
            webhook_order_id,
            provider_payment.provider_payment_id,
        )
        await db.flush()

        return WebhookProcessingResult(outcome, order_id)
