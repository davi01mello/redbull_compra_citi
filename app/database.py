from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from fastapi import HTTPException
from sqlalchemy import DateTime, ForeignKey, Integer, String, create_engine, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship, sessionmaker
from sqlalchemy.pool import StaticPool

from .models import Order, OrderCreate, OrderItem, OrderStatus, PaymentData, Product


class Base(DeclarativeBase):
    pass


class ProductRow(Base):
    __tablename__ = "products"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    description: Mapped[str] = mapped_column(String(240))
    color: Mapped[str] = mapped_column(String(20))
    price_cents: Mapped[int] = mapped_column(Integer)
    stock: Mapped[int] = mapped_column(Integer)
    reserved: Mapped[int] = mapped_column(Integer, default=0)


class OrderRow(Base):
    __tablename__ = "orders"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    correlation_id: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    buyer_name: Mapped[str] = mapped_column(String(80))
    total_cents: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(30), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    provider_charge_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    br_code: Mapped[str | None] = mapped_column(String(1200), nullable=True)
    simulated: Mapped[int] = mapped_column(Integer, default=1)
    items: Mapped[list[OrderItemRow]] = relationship(back_populates="order", cascade="all, delete-orphan", lazy="selectin")


class OrderItemRow(Base):
    __tablename__ = "order_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.id"), index=True)
    product_id: Mapped[str] = mapped_column(ForeignKey("products.id"))
    product_name: Mapped[str] = mapped_column(String(100))
    quantity: Mapped[int] = mapped_column(Integer)
    unit_price_cents: Mapped[int] = mapped_column(Integer)
    order: Mapped[OrderRow] = relationship(back_populates="items")


class WebhookEventRow(Base):
    __tablename__ = "webhook_events"

    event_id: Mapped[str] = mapped_column(String(200), primary_key=True)
    event_type: Mapped[str] = mapped_column(String(80))
    processed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


INITIAL_PRODUCTS = [
    ProductRow(id="original", name="Energy Original", description="O clássico que dá asas.", color="#164194", price_cents=800, stock=24, reserved=0),
    ProductRow(id="tropical", name="Tropical", description="Sabor tropical e refrescante.", color="#f5c400", price_cents=800, stock=18, reserved=0),
    ProductRow(id="melancia", name="Melancia", description="Leve, frutado e marcante.", color="#e7354f", price_cents=800, stock=16, reserved=0),
]


class Repository:
    def __init__(self, database_url: str) -> None:
        kwargs: dict = {"pool_pre_ping": True}
        if database_url.startswith("sqlite"):
            kwargs["connect_args"] = {"check_same_thread": False}
            if database_url in {"sqlite://", "sqlite:///:memory:"}:
                kwargs["poolclass"] = StaticPool
        self.engine = create_engine(database_url, **kwargs)
        self.sessions = sessionmaker(self.engine, expire_on_commit=False)
        Base.metadata.create_all(self.engine)
        self._seed()

    def _seed(self) -> None:
        with self.sessions.begin() as session:
            if session.scalar(select(ProductRow.id).limit(1)) is None:
                for product in INITIAL_PRODUCTS:
                    session.add(ProductRow(
                        id=product.id, name=product.name, description=product.description,
                        color=product.color, price_cents=product.price_cents,
                        stock=product.stock, reserved=0,
                    ))

    @staticmethod
    def _now() -> datetime:
        return datetime.now(UTC)

    @staticmethod
    def _aware(value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value

    def _expire_locked(self, session: Session) -> None:
        now = self._now()
        rows = session.scalars(
            select(OrderRow).where(OrderRow.status == OrderStatus.PENDING, OrderRow.expires_at <= now - timedelta(minutes=5)).with_for_update()
        ).all()
        for order in rows:
            for item in order.items:
                product = session.get(ProductRow, item.product_id)
                if product:
                    product.reserved = max(0, product.reserved - item.quantity)
            order.status = OrderStatus.EXPIRED

    def list_products(self) -> list[Product]:
        with self.sessions.begin() as session:
            self._expire_locked(session)
            rows = session.scalars(select(ProductRow).order_by(ProductRow.id)).all()
            return [Product(id=p.id, name=p.name, description=p.description, color=p.color, price=p.price_cents / 100, stock=max(0, p.stock - p.reserved)) for p in rows]

    def reserve_order(self, payload: OrderCreate) -> OrderRow:
        with self.sessions.begin() as session:
            self._expire_locked(session)
            requested: dict[str, int] = {}
            for item in payload.items:
                requested[item.product_id] = requested.get(item.product_id, 0) + item.quantity
            products: dict[str, ProductRow] = {}
            for product_id, quantity in requested.items():
                product = session.scalar(select(ProductRow).where(ProductRow.id == product_id).with_for_update())
                if not product:
                    raise HTTPException(404, f"Produto '{product_id}' não encontrado")
                if quantity > product.stock - product.reserved:
                    raise HTTPException(409, f"Estoque insuficiente para {product.name}")
                products[product_id] = product
            order_id = str(uuid4())
            row = OrderRow(
                id=order_id, correlation_id=order_id, buyer_name=payload.buyer_name.strip(),
                total_cents=sum(products[pid].price_cents * qty for pid, qty in requested.items()),
                status=OrderStatus.PAYMENT_CREATING, created_at=self._now(), simulated=1,
            )
            for product_id, quantity in requested.items():
                product = products[product_id]
                product.reserved += quantity
                row.items.append(OrderItemRow(product_id=product.id, product_name=product.name, quantity=quantity, unit_price_cents=product.price_cents))
            session.add(row)
            session.flush()
            session.refresh(row)
            return row

    def attach_payment(self, order_id: str, charge_id: str, br_code: str, expires_at: datetime, simulated: bool) -> Order:
        with self.sessions.begin() as session:
            row = session.scalar(select(OrderRow).where(OrderRow.id == order_id).with_for_update())
            if not row:
                raise HTTPException(404, "Pedido não encontrado")
            row.provider_charge_id = charge_id
            row.br_code = br_code
            row.expires_at = expires_at
            row.simulated = int(simulated)
            row.status = OrderStatus.PENDING
            session.flush()
            return self._to_order(row)

    def fail_order(self, order_id: str) -> None:
        with self.sessions.begin() as session:
            row = session.scalar(select(OrderRow).where(OrderRow.id == order_id).with_for_update())
            if not row or row.status not in {OrderStatus.PAYMENT_CREATING, OrderStatus.PENDING}:
                return
            for item in row.items:
                product = session.get(ProductRow, item.product_id)
                if product:
                    product.reserved = max(0, product.reserved - item.quantity)
            row.status = OrderStatus.FAILED

    def get_order(self, order_id: str) -> Order:
        with self.sessions.begin() as session:
            self._expire_locked(session)
            row = session.get(OrderRow, order_id)
            if not row:
                raise HTTPException(404, "Pedido não encontrado")
            return self._to_order(row)

    def get_correlation(self, correlation_id: str) -> Order | None:
        with self.sessions() as session:
            row = session.scalar(select(OrderRow).where(OrderRow.correlation_id == correlation_id))
            return self._to_order(row) if row else None

    def get_br_code(self, order_id: str) -> str:
        with self.sessions() as session:
            row = session.get(OrderRow, order_id)
            if not row or not row.br_code:
                raise HTTPException(404, "QR Code não encontrado")
            return row.br_code

    def confirm_payment(self, correlation_id: str, event_id: str, event_type: str, paid_value_cents: int) -> tuple[Order, bool]:
        with self.sessions.begin() as session:
            existing = session.get(WebhookEventRow, event_id)
            row = session.scalar(select(OrderRow).where(OrderRow.correlation_id == correlation_id).with_for_update())
            if not row:
                raise HTTPException(404, "Pedido não encontrado")
            if existing or row.status == OrderStatus.PAID:
                return self._to_order(row), False
            if paid_value_cents != row.total_cents:
                raise HTTPException(409, "Valor pago difere do valor do pedido")
            if row.status not in {OrderStatus.PENDING, OrderStatus.EXPIRED}:
                raise HTTPException(409, "Pedido não está aguardando pagamento")
            for item in row.items:
                product = session.scalar(select(ProductRow).where(ProductRow.id == item.product_id).with_for_update())
                if not product or product.stock < item.quantity:
                    raise HTTPException(409, "Estoque inconsistente para concluir o pagamento")
                product.stock -= item.quantity
                product.reserved = max(0, product.reserved - item.quantity)
            row.status = OrderStatus.PAID
            row.paid_at = self._now()
            session.add(WebhookEventRow(event_id=event_id, event_type=event_type, processed_at=self._now()))
            session.flush()
            return self._to_order(row), True

    def expire_order(self, correlation_id: str) -> bool:
        with self.sessions.begin() as session:
            row = session.scalar(select(OrderRow).where(OrderRow.correlation_id == correlation_id).with_for_update())
            if not row or row.status != OrderStatus.PENDING:
                return False
            for item in row.items:
                product = session.get(ProductRow, item.product_id)
                if product:
                    product.reserved = max(0, product.reserved - item.quantity)
            row.status = OrderStatus.EXPIRED
            return True

    @staticmethod
    def _items(row: OrderRow) -> list[OrderItem]:
        return [OrderItem(product_id=i.product_id, product_name=i.product_name, quantity=i.quantity, unit_price=i.unit_price_cents / 100, subtotal=i.unit_price_cents * i.quantity / 100) for i in row.items]

    def _to_order(self, row: OrderRow) -> Order:
        payment = None
        if row.provider_charge_id and row.br_code:
            payment = PaymentData(
                txid=row.correlation_id,
                qr_code=f"/api/orders/{row.id}/qr",
                copy_paste=row.br_code,
                simulated=bool(row.simulated),
                automatic_confirmation=not row.provider_charge_id.startswith("STATIC-"),
                expires_at=self._aware(row.expires_at),
            )
        return Order(
            id=UUID(row.id), buyer_name=row.buyer_name, items=self._items(row),
            total=row.total_cents / 100, status=OrderStatus(row.status),
            created_at=self._aware(row.created_at), paid_at=self._aware(row.paid_at), payment=payment,
        )


def create_repository(database_url: str) -> Repository:
    if database_url.startswith("postgresql://"):
        database_url = database_url.replace("postgresql://", "postgresql+psycopg://", 1)
    elif database_url.startswith("postgres://"):
        database_url = database_url.replace("postgres://", "postgresql+psycopg://", 1)
    return Repository(database_url)
