from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
import hashlib
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


@dataclass(frozen=True)
class ReconciliationSummary:
    considered: int
    approved: int
    pending: int
    terminal: int
    failed: int


def reconciliation_event_id(prefix: str, provider_order_id: str) -> str:
    candidate = f"{prefix}:{provider_order_id}"
    if len(candidate) <= 120:
        return candidate
    digest = hashlib.sha256(provider_order_id.encode()).hexdigest()
    return f"{prefix[:50]}:{digest}"


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
) -> tuple[Decimal, str, bool]:
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
                if not order.payer_email:
                    raise PaymentServiceError("O pedido não possui e-mail do pagador")
                return Decimal(payment.amount), order.payer_email, True
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
        if not order.payer_email:
            raise PaymentServiceError("O pedido não possui e-mail do pagador")

        await db.flush()
        return Decimal(order.total), order.payer_email, False


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
    amount, payer_email, already_created = await _prepare_payment(
        db, order_id, provider
    )
    if already_created:
        return await _load_order(db, order_id)

    try:
        charge = await provider.create_charge(
            external_reference=str(order_id),
            amount=amount,
            idempotency_key=payment_idempotency_key(order_id),
            payer_email=payer_email,
        )
        logger.info(
            "Cobrança criada: order_id=%s provider_order_id=%s "
            "provider_payment_id=%s",
            order_id,
            charge.provider_order_id,
            charge.provider_payment_id,
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
        if provider_name == PaymentProviderName.MERCADOPAGO:
            try:
                provider_payment = await provider.get_order(provider_order_id)
            except PaymentProviderError:
                provider_payment = None
            if provider_payment is not None:
                reconciliation = await process_mercadopago_payment(
                    db,
                    event_id=reconciliation_event_id(
                        "cancel-reconciliation", provider_order_id
                    ),
                    event_type="cancel.reconciliation",
                    webhook_order_id=provider_order_id,
                    provider_payment=provider_payment,
                )
                if reconciliation.processed or reconciliation.outcome == "already_paid":
                    raise PaymentCancellationConflict(
                        "O pagamento foi aprovado antes do cancelamento e já foi confirmado."
                    ) from exc
                if reconciliation.outcome in {
                    "remote_cancelled",
                    "remote_expired",
                    "already_terminal",
                }:
                    return await _load_order(db, order_id)
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
            await _release_locked_reservation(db, order)

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
    safe_event_id = (
        event_id
        if len(event_id) <= 120
        else reconciliation_event_id("webhook-event", event_id)
    )
    statement = (
        insert(WebhookEventORM)
        .values(
            provider=PaymentProviderName.MERCADOPAGO,
            event_id=safe_event_id,
            event_type=event_type[:120],
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


async def _release_locked_reservation(
    db: AsyncSession,
    order: OrderORM,
) -> None:
    if order.reservation_status != ReservationStatus.ACTIVE:
        return
    quantities = await _load_quantities(db, order.id)
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


async def reconcile_pending_mercadopago_payments(
    db: AsyncSession,
    provider: PaymentProvider,
    limit: int = 50,
) -> ReconciliationSummary:
    if _provider_name(provider) != PaymentProviderName.MERCADOPAGO:
        return ReconciliationSummary(0, 0, 0, 0, 0)

    safe_limit = max(1, min(limit, 200))
    async with db.begin():
        candidates = list(
            (
                await db.execute(
                    select(PaymentORM.provider_order_id)
                    .where(
                        PaymentORM.provider == PaymentProviderName.MERCADOPAGO,
                        PaymentORM.status.in_(
                            {
                                PaymentStatus.CREATING,
                                PaymentStatus.WAITING_PAYMENT,
                            }
                        ),
                        PaymentORM.provider_order_id.is_not(None),
                    )
                    .order_by(PaymentORM.updated_at, PaymentORM.order_id)
                    .limit(safe_limit)
                )
            ).scalars()
        )

    approved = pending = terminal = failed = 0
    for provider_order_id in candidates:
        if not provider_order_id:
            continue
        try:
            provider_payment = await provider.get_order(provider_order_id)
            result = await process_mercadopago_payment(
                db,
                event_id=reconciliation_event_id(
                    f"periodic-reconciliation-{provider_payment.status.lower()}",
                    provider_order_id,
                ),
                event_type="payment.reconciliation",
                webhook_order_id=provider_order_id,
                provider_payment=provider_payment,
            )
            if result.processed or result.outcome == "already_paid":
                approved += 1
            elif result.outcome in {
                "remote_cancelled",
                "remote_expired",
                "already_terminal",
            }:
                terminal += 1
            else:
                pending += 1
            logger.info(
                "Reconciliação Mercado Pago: provider_order_id=%s "
                "order_id=%s outcome=%s",
                provider_order_id,
                result.order_id,
                result.outcome,
            )
        except Exception as exc:
            failed += 1
            logger.warning(
                "Falha na reconciliação Mercado Pago: "
                "provider_order_id=%s error=%s",
                provider_order_id,
                type(exc).__name__,
            )
            if db.in_transaction():
                await db.rollback()

    return ReconciliationSummary(
        considered=len(candidates),
        approved=approved,
        pending=pending,
        terminal=terminal,
        failed=failed,
    )


async def expire_due_payments(
    db: AsyncSession,
    as_of: datetime | None = None,
    provider: PaymentProvider | None = None,
) -> int:
    cutoff = as_of or datetime.now(UTC)
    configured_provider = _provider_name(provider) if provider is not None else None
    if (
        provider is not None
        and configured_provider == PaymentProviderName.MERCADOPAGO
    ):
        summary = await reconcile_pending_mercadopago_payments(db, provider)
        if summary.considered:
            logger.info(
                "Reconciliação anterior à expiração: considered=%s "
                "approved=%s pending=%s terminal=%s failed=%s",
                summary.considered,
                summary.approved,
                summary.pending,
                summary.terminal,
                summary.failed,
            )

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
            if not provider_order_id:
                logger.warning(
                    "Cobrança vencida sem provider_order_id; expiração adiada: "
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
                    remote_result: WebhookProcessingResult | None = None
                    if configured_provider == PaymentProviderName.MERCADOPAGO:
                        try:
                            provider_payment = await provider.get_order(
                                provider_order_id
                            )
                            remote_result = await process_mercadopago_payment(
                                db,
                                event_id=reconciliation_event_id(
                                    "expiration-reconciliation",
                                    provider_order_id,
                                ),
                                event_type="expiration.reconciliation",
                                webhook_order_id=provider_order_id,
                                provider_payment=provider_payment,
                            )
                        except PaymentProviderError:
                            remote_result = None
                    logger.warning(
                        "Não foi possível cancelar cobrança vencida no provedor: "
                        "order_id=%s provider_order_id=%s outcome=%s error=%s",
                        order_id,
                        provider_order_id,
                        remote_result.outcome if remote_result else None,
                        type(exc).__name__,
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

            await _release_locked_reservation(db, order)

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
        remote_status = provider_payment.status.upper()
        terminal_statuses = {
            "CANCELLED": PaymentStatus.CANCELLED,
            "EXPIRED": PaymentStatus.EXPIRED,
        }
        if remote_status not in {"APPROVED", *terminal_statuses}:
            await _record_webhook_event(
                db,
                event_id,
                event_type,
                webhook_order_id,
                provider_payment.provider_payment_id,
            )
            return WebhookProcessingResult("not_approved", order_id)
        if remote_status == "APPROVED" and provider_payment.approved_at is None:
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
        if remote_status in terminal_statuses:
            target_status = terminal_statuses[remote_status]
            if payment.status == target_status:
                await _record_webhook_event(
                    db,
                    event_id,
                    event_type,
                    webhook_order_id,
                    provider_payment.provider_payment_id,
                )
                return WebhookProcessingResult("already_terminal", order_id)
            if payment.status not in {
                PaymentStatus.CREATING,
                PaymentStatus.WAITING_PAYMENT,
            }:
                await _record_webhook_event(
                    db,
                    event_id,
                    event_type,
                    webhook_order_id,
                    provider_payment.provider_payment_id,
                )
                return WebhookProcessingResult(
                    "inconsistent_payment_state", order_id
                )
            await _release_locked_reservation(db, order)
            payment.status = target_status
            payment.updated_at = datetime.now(UTC)
            if remote_status == "CANCELLED":
                order.status = OrderStatus.CANCELLED
            await _record_webhook_event(
                db,
                event_id,
                event_type,
                webhook_order_id,
                provider_payment.provider_payment_id,
            )
            await db.flush()
            return WebhookProcessingResult(
                "remote_cancelled"
                if remote_status == "CANCELLED"
                else "remote_expired",
                order_id,
            )
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
            if conflicts:
                await _flag_manual_review(
                    db,
                    order_id,
                    ManualReviewReason.LATE_PAYMENT_STOCK_CONFLICT,
                    "Sem estoque não reservado suficiente para: "
                    + ", ".join(conflicts),
                )
                outcome = "approved_late_stock_conflict"
            else:
                for product_id in product_ids:
                    products[product_id].stock -= quantities[product_id]
                outcome = (
                    "approved_after_cancellation"
                    if cancelled_by_buyer
                    else "approved_late"
                )
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
        if provider_payment.provider_payment_id:
            payment.provider_payment_id = provider_payment.provider_payment_id
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
