from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from .db.models import OrderItemORM, OrderORM, ProductORM
from .models import Order, OrderCreate, OrderItem, OrderStatus, PaymentData, Product


def unit_price_for_quantity(quantity: int) -> float:
    if quantity >= 4:
        return 7.5
    if quantity >= 2:
        return 8.0
    return 8.5


def _product_to_model(product: ProductORM) -> Product:
    return Product(
        id=product.id,
        name=product.name,
        description=product.description,
        color=product.color,
        price=float(product.price),
        stock=product.stock,
    )


def _order_to_model(order: OrderORM) -> Order:
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
        created_at=order.created_at,
        paid_at=order.paid_at,
        payment=PaymentData(
            txid=order.txid,
            qr_code=order.qr_code,
            copy_paste=order.copy_paste,
        ),
    )


class DatabaseStore:
    async def list_products(self, db: AsyncSession) -> list[Product]:
        result = await db.execute(select(ProductORM).order_by(ProductORM.id))
        return [_product_to_model(product) for product in result.scalars()]

    async def create_order(self, db: AsyncSession, payload: OrderCreate) -> Order:
        requested: dict[str, int] = {}
        for item in payload.items:
            requested[item.product_id] = requested.get(item.product_id, 0) + item.quantity

        async with db.begin():
            result = await db.execute(
                select(ProductORM).where(ProductORM.id.in_(requested))
            )
            products = {product.id: product for product in result.scalars()}

            total_quantity = sum(requested.values())
            unit_price = Decimal(str(unit_price_for_quantity(total_quantity)))
            items: list[OrderItemORM] = []

            for product_id, quantity in requested.items():
                product = products.get(product_id)
                if not product:
                    raise HTTPException(404, f"Produto '{product_id}' não encontrado")
                if quantity > product.stock:
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

            order_id = uuid4()
            txid = f"SIM-{order_id.hex[:16].upper()}"
            total = sum((item.subtotal for item in items), start=Decimal("0.00"))
            order = OrderORM(
                id=order_id,
                buyer_name=payload.buyer_name.strip(),
                items=items,
                total=total,
                status=OrderStatus.PENDING,
                created_at=datetime.now(UTC),
                txid=txid,
                qr_code=f"/api/mock/qr/{txid}",
                copy_paste=f"00020126PIX-SIMULADO-{txid}-VALOR-{total:.2f}",
            )
            db.add(order)
            await db.flush()

        return _order_to_model(order)

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
