from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from fastapi import HTTPException
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from .db.models import OrderItemORM, OrderORM, PriceTierORM, ProductORM
from .models import (
    Order,
    OrderCreate,
    OrderItem,
    OrderStatus,
    PaymentData,
    PriceTier,
    PricingAudience,
    Product,
    ReservationStatus,
)


class ChargeCreationError(RuntimeError):
    pass


def _product_to_model(product: ProductORM) -> Product:
    return Product(
        id=product.id,
        name=product.name,
        description=product.description,
        color=product.color,
        price=float(product.price),
        stock=product.stock - product.reserved_stock,
    )


def _order_to_model(order: OrderORM) -> Order:
    if order.price_tier_id is None:
        raise RuntimeError(f"Pedido {order.id} não possui faixa de preço")
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
            txid=order.txid,
            qr_code=order.qr_code,
            copy_paste=order.copy_paste,
        ),
    )


class DatabaseStore:
    async def _create_mock_charge(
        self,
        order_id: UUID,
        total: Decimal,
    ) -> PaymentData:
        txid = f"SIM-{order_id.hex[:16].upper()}"
        return PaymentData(
            txid=txid,
            qr_code=f"/api/mock/qr/{txid}",
            copy_paste=f"00020126PIX-SIMULADO-{txid}-VALOR-{total:.2f}",
        )

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

    async def create_order(self, db: AsyncSession, payload: OrderCreate) -> Order:
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
            reservation_txid = f"RES-{order_id.hex.upper()}"
            total = sum((item.subtotal for item in items), start=Decimal("0.00"))
            order = OrderORM(
                id=order_id,
                buyer_name=payload.buyer_name.strip(),
                items=items,
                total=total,
                status=OrderStatus.PENDING,
                created_at=datetime.now(UTC),
                txid=reservation_txid,
                qr_code="",
                copy_paste="",
                pricing_audience=payload.pricing_audience,
                price_tier_id=price_tier.id,
                reservation_status=ReservationStatus.ACTIVE,
            )
            db.add(order)
            await db.flush()

        try:
            payment = await self._create_mock_charge(order_id, total)
        except ChargeCreationError as exc:
            await self.release_reservation(db, order_id)
            raise HTTPException(
                502,
                "Não foi possível criar a cobrança; a reserva foi liberada",
            ) from exc

        async with db.begin():
            result = await db.execute(
                select(OrderORM).where(OrderORM.id == order_id).with_for_update()
            )
            order = result.scalar_one()
            order.txid = payment.txid
            order.qr_code = payment.qr_code
            order.copy_paste = payment.copy_paste
            await db.flush()

        return _order_to_model(order)

    async def release_reservation(
        self,
        db: AsyncSession,
        order_id: UUID,
    ) -> bool:
        async with db.begin():
            order_result = await db.execute(
                select(OrderORM).where(OrderORM.id == order_id).with_for_update()
            )
            order = order_result.scalar_one_or_none()
            if order is None or order.reservation_status != ReservationStatus.ACTIVE:
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
            await db.flush()

        return True

    async def get_order(self, db: AsyncSession, order_id: str) -> Order:
        try:
            parsed_order_id = UUID(order_id)
        except ValueError:
            raise HTTPException(404, "Pedido não encontrado") from None

        result = await db.execute(
            select(OrderORM)
            .options(selectinload(OrderORM.items))
            .where(OrderORM.id == parsed_order_id)
        )
        order = result.scalar_one_or_none()
        if not order:
            raise HTTPException(404, "Pedido não encontrado")
        return _order_to_model(order)

    async def confirm_payment(
        self, db: AsyncSession, txid: str
    ) -> tuple[Order, bool]:
        async with db.begin():
            result = await db.execute(
                select(OrderORM).where(OrderORM.txid == txid).with_for_update()
            )
            order = result.scalar_one_or_none()
            if not order:
                raise HTTPException(404, "Transação não encontrada")

            await db.refresh(order, attribute_names=["items"])
            if order.status == OrderStatus.PAID:
                return _order_to_model(order), False

            product_ids = sorted({item.product_id for item in order.items})
            products_result = await db.execute(
                select(ProductORM)
                .where(ProductORM.id.in_(product_ids))
                .order_by(ProductORM.id)
                .with_for_update()
            )
            products = {product.id: product for product in products_result.scalars()}

            for item in order.items:
                if products[item.product_id].stock < item.quantity:
                    raise HTTPException(
                        409, "Estoque insuficiente para concluir o pagamento"
                    )
            for item in order.items:
                products[item.product_id].stock -= item.quantity

            order.status = OrderStatus.PAID
            order.paid_at = datetime.now(UTC)
            await db.flush()
            response = _order_to_model(order)

        return response, True


store = DatabaseStore()
