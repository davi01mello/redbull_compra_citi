import asyncio
from copy import deepcopy
from datetime import UTC, datetime
from uuid import uuid4

from fastapi import HTTPException

from .models import Order, OrderCreate, OrderItem, OrderStatus, PaymentData, Product



INITIAL_PRODUCTS = {
    "original": Product(id="original", name="Energy Original", description="O clássico que dá asas.", color="#164194", price=8.5, stock=24),
    "tropical": Product(id="tropical", name="Tropical", description="Sabor tropical e refrescante.", color="#f5c400", price=8.5, stock=18),
    "melancia": Product(id="melancia", name="Melancia", description="Leve, frutado e marcante.", color="#e7354f", price=8.5, stock=16),
}


def unit_price_for_quantity(quantity: int) -> float:
    if quantity >= 4:
        return 7.5
    if quantity >= 2:
        return 8.0
    return 8.5


class MemoryStore:
    """Substituto temporário do banco; o lock simula uma transação atômica."""

    def __init__(self) -> None:
        self.products = deepcopy(INITIAL_PRODUCTS)
        self.orders: dict[str, Order] = {}
        self._lock = asyncio.Lock()

    def list_products(self) -> list[Product]:
        return list(self.products.values())

    async def create_order(self, payload: OrderCreate) -> Order:
        async with self._lock:
            requested: dict[str, int] = {}
            for item in payload.items:
                requested[item.product_id] = requested.get(item.product_id, 0) + item.quantity

            total_quantity = sum(requested.values())
            unit_price = unit_price_for_quantity(total_quantity)
            items: list[OrderItem] = []
            for product_id, quantity in requested.items():
                product = self.products.get(product_id)
                if not product:
                    raise HTTPException(404, f"Produto '{product_id}' não encontrado")
                if quantity > product.stock:
                    raise HTTPException(409, f"Estoque insuficiente para {product.name}")
                items.append(OrderItem(product_id=product.id, product_name=product.name, quantity=quantity, unit_price=unit_price, subtotal=round(unit_price * quantity, 2)))

            order_id = uuid4()
            txid = f"SIM-{order_id.hex[:16].upper()}"
            total = round(sum(item.subtotal for item in items), 2)
            order = Order(
                id=order_id,
                buyer_name=payload.buyer_name.strip(),
                items=items,
                total=total,
                status=OrderStatus.PENDING,
                created_at=datetime.now(UTC),
                payment=PaymentData(
                    txid=txid,
                    qr_code=f"/api/mock/qr/{txid}",
                    copy_paste=f"00020126PIX-SIMULADO-{txid}-VALOR-{total:.2f}",
                ),
            )
            self.orders[str(order.id)] = order
            return order

    def get_order(self, order_id: str) -> Order:
        order = self.orders.get(order_id)
        if not order:
            raise HTTPException(404, "Pedido não encontrado")
        return order

    async def confirm_payment(self, txid: str) -> tuple[Order, bool]:
        async with self._lock:
            order = next((item for item in self.orders.values() if item.payment.txid == txid), None)
            if not order:
                raise HTTPException(404, "Transação não encontrada")
            if order.status == OrderStatus.PAID:
                return order, False
            for item in order.items:
                if self.products[item.product_id].stock < item.quantity:
                    raise HTTPException(409, "Estoque insuficiente para concluir o pagamento")
            for item in order.items:
                self.products[item.product_id].stock -= item.quantity
            order.status = OrderStatus.PAID
            order.paid_at = datetime.now(UTC)
            return order, True


store = MemoryStore()
