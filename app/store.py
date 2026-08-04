from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from fastapi import HTTPException
from sqlalchemy import inspect, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import NO_VALUE
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
    loaded_items = inspect(order).attrs["items"].loaded_value
    items_source = (
        loaded_items
        if loaded_items is not NO_VALUE and loaded_items
        else order.pending_items or []
    )
    return Order(
        id=order.id,
        buyer_name=order.buyer_name,
        items=[
            OrderItem(
                product_id=item.product_id if hasattr(item, 'product_id') else item['product_id'],
                product_name=item.product_name if hasattr(item, 'product_name') else item['product_name'],
                quantity=item.quantity if hasattr(item, 'quantity') else item['quantity'],
                unit_price=float(item.unit_price if hasattr(item, 'unit_price') else item['unit_price']),
                subtotal=float(item.subtotal if hasattr(item, 'subtotal') else item['subtotal']),
            )
            for item in items_source
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
            pending_items: list[dict] = []

            for product_id, quantity in requested.items():
                product = products.get(product_id)
                if not product:
                    raise HTTPException(404, f"Produto '{product_id}' não encontrado")
                if quantity > product.stock:
                    raise HTTPException(409, f"Estoque insuficiente para {product.name}")

                pending_items.append({
                    "product_id": product.id,
                    "product_name": product.name,
                    "quantity": quantity,
                    "unit_price": float(unit_price),
                    "subtotal": float(unit_price * quantity),
                })

            order_id = uuid4()
            txid = f"SIM-{order_id.hex[:16].upper()}"
            total = sum(Decimal(str(item["subtotal"])) for item in pending_items)
            order = OrderORM(
                id=order_id,
                buyer_name=payload.buyer_name.strip(),
                pending_items=pending_items,
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
            if order.status == OrderStatus.CANCELLED:
                raise HTTPException(409, "Pedido cancelado não pode ser pago")

            items_source = order.items if order.items else order.pending_items or []
            product_ids = sorted({item.product_id if hasattr(item, 'product_id') else item['product_id'] for item in items_source})
            products_result = await db.execute(
                select(ProductORM)
                .where(ProductORM.id.in_(product_ids))
                .order_by(ProductORM.id)
                .with_for_update()
            )
            products = {product.id: product for product in products_result.scalars()}

            for item in items_source:
                product_id = item.product_id if hasattr(item, 'product_id') else item['product_id']
                quantity = item.quantity if hasattr(item, 'quantity') else item['quantity']
                if products[product_id].stock < quantity:
                    raise HTTPException(
                        409, "Estoque insuficiente para concluir o pagamento"
                    )
            for item in items_source:
                product_id = item.product_id if hasattr(item, 'product_id') else item['product_id']
                quantity = item.quantity if hasattr(item, 'quantity') else item['quantity']
                products[product_id].stock -= quantity

            if not order.items:
                order.items = [
                    OrderItemORM(
                        order_id=order.id,
                        product_id=item['product_id'],
                        product_name=item['product_name'],
                        quantity=item['quantity'],
                        unit_price=Decimal(str(item['unit_price'])),
                        subtotal=Decimal(str(item['subtotal'])),
                    )
                    for item in order.pending_items
                ]

            order.status = OrderStatus.PAID
            order.paid_at = datetime.now(UTC)
            await db.flush()
            response = _order_to_model(order)

        return response, True

    async def cancel_order(self, db: AsyncSession, order_id: str) -> Order:
        try:
            parsed_order_id = UUID(order_id)
        except ValueError:
            raise HTTPException(404, "Pedido não encontrado") from None

        async with db.begin():
            result = await db.execute(
                select(OrderORM).where(OrderORM.id == parsed_order_id).with_for_update()
            )
            order = result.scalar_one_or_none()
            if not order:
                raise HTTPException(404, "Pedido não encontrado")

            if order.status == OrderStatus.PAID:
                raise HTTPException(409, "Não é possível cancelar um pedido já pago")
            if order.status == OrderStatus.CANCELLED:
                return _order_to_model(order)

            order.status = OrderStatus.CANCELLED
            await db.flush()
            return _order_to_model(order)


store = DatabaseStore()
