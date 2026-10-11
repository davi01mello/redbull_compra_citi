from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from fastapi import HTTPException
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from .db.models import (
    OrderItemORM,
    OrderORM,
    PaymentORM,
    PriceTierORM,
    ProductORM,
)
from .models import (
    Order,
    OrderCreate,
    OrderItem,
    OrderStatus,
    PaymentData,
    PaymentProvider,
    PaymentStatus,
    PriceTier,
    PricingAudience,
    Product,
    ReservationStatus,
    StockMovementSource,
    StockMovementType,
)
from .stock_service import set_stock_context


@dataclass(frozen=True)
class ReservedOrder:
    id: UUID
    total: Decimal


def _product_to_model(product: ProductORM) -> Product:
    return Product(
        id=product.id,
        name=product.name,
        stock=product.stock - product.reserved_stock,
        category=product.category,
    )


def _order_to_model(order: OrderORM) -> Order:
    if order.price_tier_id is None:
        raise RuntimeError(f"Pedido {order.id} não possui faixa de preço")
    payment = order.payment
    if payment is None:
        raise RuntimeError(f"Pedido {order.id} não possui pagamento")
    payment_reference = payment.provider_payment_id or payment.provider_order_id
    if (
        not payment_reference
        or payment.copy_paste is None
        or payment.qr_code is None
    ):
        raise RuntimeError(f"Pagamento do pedido {order.id} está incompleto")
    return Order(
        id=order.id,
        buyer_name=order.buyer_name,
        items=[
            OrderItem(
                product_id=item.product_id,
                product_name=item.product_name,
                quantity=item.quantity,
                unit_price=float(item.unit_price),
                subtotal=float(item.subtotal),
            )
            for item in order.items
        ],
        total=float(order.total),
        status=order.status,
        pricing_audience=order.pricing_audience,
        price_tier_id=order.price_tier_id,
        created_at=order.created_at,
        paid_at=order.paid_at,
        payment=PaymentData(
            txid=payment_reference,
            qr_code=f"/api/orders/{order.id}/qr",
            copy_paste=payment.copy_paste,
            status=payment.status,
            expires_at=payment.expires_at,
            simulated=payment.provider == PaymentProvider.MOCK,
        ),
    )


class DatabaseStore:
    async def list_products(self, db: AsyncSession) -> list[Product]:
        result = await db.execute(select(ProductORM).order_by(ProductORM.id))
        return [_product_to_model(product) for product in result.scalars()]

    async def list_price_tiers(
        self,
        db: AsyncSession,
        pricing_audience: PricingAudience,
    ) -> list[PriceTier]:
        result = await db.execute(
            select(PriceTierORM)
            .where(
                PriceTierORM.pricing_audience == pricing_audience,
                PriceTierORM.active.is_(True),
            )
            .order_by(PriceTierORM.min_quantity)
        )
        return [
            PriceTier(
                id=tier.id,
                pricing_audience=tier.pricing_audience,
                min_quantity=tier.min_quantity,
                max_quantity=tier.max_quantity,
                unit_price=float(tier.unit_price),
            )
            for tier in result.scalars()
        ]

    async def reserve_order(
        self,
        db: AsyncSession,
        payload: OrderCreate,
    ) -> ReservedOrder:
        requested: dict[str, int] = {}
        for item in payload.items:
            requested[item.product_id] = requested.get(item.product_id, 0) + item.quantity

        async with db.begin():
            total_quantity = sum(requested.values())
            tier_result = await db.execute(
                select(PriceTierORM)
                .where(
                    PriceTierORM.pricing_audience == payload.pricing_audience,
                    PriceTierORM.active.is_(True),
                    PriceTierORM.min_quantity <= total_quantity,
                    or_(
                        PriceTierORM.max_quantity.is_(None),
                        PriceTierORM.max_quantity >= total_quantity,
                    ),
                )
                .order_by(PriceTierORM.min_quantity.desc())
                .limit(1)
            )
            price_tier = tier_result.scalar_one_or_none()
            if price_tier is None:
                raise HTTPException(
                    422,
                    "Nenhuma faixa de preço ativa cobre este pedido",
                )

            product_ids = sorted(requested)
            result = await db.execute(
                select(ProductORM)
                .where(ProductORM.id.in_(product_ids))
                .order_by(ProductORM.id)
                .with_for_update()
            )
            products = {product.id: product for product in result.scalars()}

            unit_price = Decimal(price_tier.unit_price)
            items: list[OrderItemORM] = []

            for product_id in product_ids:
                quantity = requested[product_id]
                product = products.get(product_id)
                if not product:
                    raise HTTPException(404, f"Produto '{product_id}' não encontrado")
                available_stock = product.stock - product.reserved_stock
                if quantity > available_stock:
                    raise HTTPException(409, f"Estoque insuficiente para {product.name}")

                items.append(
                    OrderItemORM(
                        product_id=product.id,
                        product_name=product.name,
                        quantity=quantity,
                        unit_price=unit_price,
                        subtotal=unit_price * quantity,
                    )
                )

            for product_id in product_ids:
                products[product_id].reserved_stock += requested[product_id]

            order_id = uuid4()
            total = sum((item.subtotal for item in items), start=Decimal("0.00"))
            order = OrderORM(
                id=order_id,
                buyer_name=payload.buyer_name,
                payer_email=payload.payer_email,
                items=items,
                total=total,
                status=OrderStatus.PENDING,
                created_at=datetime.now(UTC),
                pricing_audience=payload.pricing_audience,
                price_tier_id=price_tier.id,
                reservation_status=ReservationStatus.ACTIVE,
            )
            db.add(order)
            await db.flush()

        return ReservedOrder(id=order_id, total=total)

    async def release_reservation(
        self,
        db: AsyncSession,
        order_id: UUID,
        payment_status: PaymentStatus | None = None,
    ) -> bool:
        async with db.begin():
            order_result = await db.execute(
                select(OrderORM).where(OrderORM.id == order_id).with_for_update()
            )
            order = order_result.scalar_one_or_none()
            payment = None
            if payment_status is not None:
                payment_result = await db.execute(
                    select(PaymentORM)
                    .where(PaymentORM.order_id == order_id)
                    .with_for_update()
                )
                payment = payment_result.scalar_one_or_none()
                if payment is not None and payment.status in {
                    PaymentStatus.WAITING_PAYMENT,
                    PaymentStatus.APPROVED,
                }:
                    return False

            if order is None or order.reservation_status != ReservationStatus.ACTIVE:
                if payment is not None:
                    payment.status = payment_status
                    payment.updated_at = datetime.now(UTC)
                return False

            items_result = await db.execute(
                select(OrderItemORM)
                .where(OrderItemORM.order_id == order_id)
                .order_by(OrderItemORM.product_id)
            )
            reserved_by_product: dict[str, int] = {}
            for item in items_result.scalars():
                reserved_by_product[item.product_id] = (
                    reserved_by_product.get(item.product_id, 0) + item.quantity
                )

            product_ids = sorted(reserved_by_product)
            products_result = await db.execute(
                select(ProductORM)
                .where(ProductORM.id.in_(product_ids))
                .order_by(ProductORM.id)
                .with_for_update()
            )
            products = {
                product.id: product for product in products_result.scalars()
            }

            for product_id in product_ids:
                quantity = reserved_by_product[product_id]
                product = products.get(product_id)
                if product is None or product.reserved_stock < quantity:
                    raise RuntimeError(
                        f"Reserva inconsistente para o produto '{product_id}'"
                    )

            for product_id in product_ids:
                products[product_id].reserved_stock -= reserved_by_product[product_id]

            order.reservation_status = ReservationStatus.RELEASED
            if payment is not None:
                payment.status = payment_status
                payment.updated_at = datetime.now(UTC)
            await db.flush()

        return True

    async def get_order(self, db: AsyncSession, order_id: str) -> Order:
        try:
            parsed_order_id = UUID(order_id)
        except ValueError:
            raise HTTPException(404, "Pedido não encontrado") from None

        result = await db.execute(
            select(OrderORM)
            .options(
                selectinload(OrderORM.items),
                selectinload(OrderORM.payment),
            )
            .where(OrderORM.id == parsed_order_id)
            .execution_options(populate_existing=True)
        )
        order = result.scalar_one_or_none()
        if not order:
            raise HTTPException(404, "Pedido não encontrado")
        return _order_to_model(order)

    async def confirm_payment(
        self, db: AsyncSession, txid: str
    ) -> tuple[Order, bool]:
        async with db.begin():
            payment_reference = (
                await db.execute(
                    select(PaymentORM.order_id).where(
                        or_(
                            PaymentORM.provider_payment_id == txid,
                            PaymentORM.provider_order_id == txid,
                        )
                    )
                )
            ).scalar_one_or_none()
            if payment_reference is None:
                raise HTTPException(404, "Transação não encontrada")

            result = await db.execute(
                select(OrderORM)
                .options(
                    selectinload(OrderORM.items),
                    selectinload(OrderORM.payment),
                )
                .where(OrderORM.id == payment_reference)
                .with_for_update()
            )
            order = result.scalar_one_or_none()
            if not order:
                raise HTTPException(404, "Transação não encontrada")

            payment = (
                await db.execute(
                    select(PaymentORM)
                    .where(PaymentORM.order_id == order.id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            ).scalar_one()
            if order.status == OrderStatus.PAID:
                return _order_to_model(order), False
            if order.reservation_status != ReservationStatus.ACTIVE:
                raise HTTPException(409, "A reserva do pedido não está ativa")

            quantities: dict[str, int] = {}
            for item in order.items:
                quantities[item.product_id] = (
                    quantities.get(item.product_id, 0) + item.quantity
                )
            product_ids = sorted(quantities)
            products_result = await db.execute(
                select(ProductORM)
                .where(ProductORM.id.in_(product_ids))
                .order_by(ProductORM.id)
                .with_for_update()
            )
            products = {product.id: product for product in products_result.scalars()}

            for product_id in product_ids:
                product = products.get(product_id)
                quantity = quantities[product_id]
                if (
                    product is None
                    or product.stock < quantity
                    or product.reserved_stock < quantity
                ):
                    raise HTTPException(
                        409, "Estoque insuficiente para concluir o pagamento"
                    )
            await set_stock_context(
                db,
                movement_type=StockMovementType.MARKETPLACE_SALE,
                source=StockMovementSource.MARKETPLACE,
                order_id=order.id,
            )
            for product_id in product_ids:
                quantity = quantities[product_id]
                products[product_id].stock -= quantity
                products[product_id].reserved_stock -= quantity

            paid_at = datetime.now(UTC)
            payment.status = PaymentStatus.APPROVED
            payment.approved_at = paid_at
            payment.updated_at = paid_at
            order.status = OrderStatus.PAID
            order.paid_at = paid_at
            order.reservation_status = ReservationStatus.CONSUMED
            await db.flush()
            response = _order_to_model(order)

        return response, True


store = DatabaseStore()
