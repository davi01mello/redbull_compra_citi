from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models import (
    OrderStatus,
    PaymentProvider,
    PaymentStatus,
    PricingAudience,
    ReservationStatus,
)

from .base import Base


class ProductORM(Base):
    __tablename__ = "products"

    id: Mapped[str] = mapped_column(String(50), primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    stock: Mapped[int] = mapped_column(Integer, nullable=False)
    reserved_stock: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
    )
    category: Mapped[str] = mapped_column(
        String,
        nullable=False,
        default="Regular",
        server_default="Regular",
    )


class OrderORM(Base):
    __tablename__ = "orders"

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    buyer_name: Mapped[str] = mapped_column(String(80), nullable=False)
    payer_email: Mapped[str | None] = mapped_column(String(254), nullable=True)
    total: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    status: Mapped[OrderStatus] = mapped_column(
        Enum(OrderStatus, name="order_status"),
        nullable=False,
        default=OrderStatus.PENDING,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    pricing_audience: Mapped[PricingAudience] = mapped_column(
        Enum(PricingAudience, name="pricing_audience"),
        nullable=False,
        default=PricingAudience.MEMBER,
        server_default=PricingAudience.MEMBER.value,
    )
    price_tier_id: Mapped[int | None] = mapped_column(
        ForeignKey("price_tiers.id"),
    )
    reservation_status: Mapped[ReservationStatus] = mapped_column(
        Enum(ReservationStatus, name="reservation_status"),
        nullable=False,
        default=ReservationStatus.RELEASED,
        server_default=ReservationStatus.RELEASED.value,
    )

    items: Mapped[list["OrderItemORM"]] = relationship(
        back_populates="order",
        cascade="all, delete-orphan",
        lazy="selectin",
    )
    price_tier: Mapped["PriceTierORM | None"] = relationship()
    payment: Mapped["PaymentORM | None"] = relationship(
        back_populates="order",
        uselist=False,
        cascade="all, delete-orphan",
        lazy="selectin",
    )
    manual_reviews: Mapped[list["ManualReviewORM"]] = relationship(
        back_populates="order",
        cascade="all, delete-orphan",
    )


class OrderItemORM(Base):
    __tablename__ = "order_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("orders.id", ondelete="CASCADE"),
        nullable=False,
    )
    product_id: Mapped[str] = mapped_column(
        String(50),
        ForeignKey("products.id"),
        nullable=False,
    )
    product_name: Mapped[str] = mapped_column(String(120), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    unit_price: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    subtotal: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)

    order: Mapped[OrderORM] = relationship(back_populates="items")


class PriceTierORM(Base):
    __tablename__ = "price_tiers"
    __table_args__ = (
        CheckConstraint("min_quantity >= 1", name="ck_price_tiers_min_quantity"),
        CheckConstraint(
            "max_quantity IS NULL OR max_quantity >= min_quantity",
            name="ck_price_tiers_quantity_range",
        ),
        CheckConstraint("unit_price > 0", name="ck_price_tiers_unit_price"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    pricing_audience: Mapped[PricingAudience] = mapped_column(
        Enum(PricingAudience, name="pricing_audience"),
        nullable=False,
    )
    min_quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    max_quantity: Mapped[int | None] = mapped_column(Integer)
    unit_price: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=text("true"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


class PaymentORM(Base):
    __tablename__ = "payments"

    id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        primary_key=True,
    )
    order_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("orders.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    provider: Mapped[PaymentProvider] = mapped_column(
        Enum(PaymentProvider, name="payment_provider"),
        nullable=False,
    )
    provider_order_id: Mapped[str | None] = mapped_column(
        String(120),
        unique=True,
    )
    provider_payment_id: Mapped[str | None] = mapped_column(
        String(120),
        unique=True,
    )
    status: Mapped[PaymentStatus] = mapped_column(
        Enum(PaymentStatus, name="payment_status"),
        nullable=False,
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    currency: Mapped[str] = mapped_column(
        String(3),
        nullable=False,
        default="BRL",
        server_default="BRL",
    )
    copy_paste: Mapped[str | None] = mapped_column(Text)
    qr_code: Mapped[str | None] = mapped_column(Text)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    order: Mapped[OrderORM] = relationship(back_populates="payment")


class WebhookEventORM(Base):
    __tablename__ = "webhook_events"

    provider: Mapped[PaymentProvider] = mapped_column(
        Enum(PaymentProvider, name="payment_provider"),
        primary_key=True,
    )
    event_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    event_type: Mapped[str] = mapped_column(String(120), nullable=False)
    provider_order_id: Mapped[str | None] = mapped_column(
        String(120),
        index=True,
    )
    provider_payment_id: Mapped[str | None] = mapped_column(String(120))
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ManualReviewORM(Base):
    __tablename__ = "manual_reviews"
    __table_args__ = (
        UniqueConstraint(
            "order_id",
            "reason",
            name="uq_manual_reviews_order_id_reason",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        primary_key=True,
    )
    order_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("orders.id", ondelete="CASCADE"),
        nullable=False,
    )
    reason: Mapped[str] = mapped_column(String(50), nullable=False)
    details: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    order: Mapped[OrderORM] = relationship(back_populates="manual_reviews")


class StaffUserORM(Base):
    """Operador do painel de estoque (tabela criada em c3f8a1d27b54)."""

    __tablename__ = "staff_users"
    __table_args__ = (
        UniqueConstraint("username", name="uq_staff_users_username"),
        CheckConstraint("role IN ('ADMIN', 'OPERADOR')", name="ck_staff_users_role"),
        CheckConstraint(
            "username ~ '^[a-z0-9._-]{3,50}$'",
            name="ck_staff_users_username_format",
        ),
        CheckConstraint(
            "failed_login_attempts >= 0",
            name="ck_staff_users_failed_login_attempts",
        ),
        {
            "comment": (
                "Operadores do painel de estoque. "
                "Senha guardada apenas como hash scrypt."
            )
        },
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(50), nullable=False)
    full_name: Mapped[str] = mapped_column(String(120), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="OPERADOR",
        server_default="OPERADOR",
    )
    active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=text("true"),
    )
    failed_login_attempts: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
    )
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


class StaffSessionORM(Base):
    """Sessão de login do painel; guarda apenas o SHA-256 do token do cookie."""

    __tablename__ = "staff_sessions"
    __table_args__ = (
        UniqueConstraint("token_hash", name="uq_staff_sessions_token_hash"),
        {
            "comment": (
                "Sessões do painel de estoque. "
                "O token do cookie nunca é salvo, só o SHA-256."
            )
        },
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    staff_user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey(
            "staff_users.id",
            ondelete="CASCADE",
            name="fk_staff_sessions_staff_user_id",
        ),
        nullable=False,
        index=True,
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    user_agent: Mapped[str | None] = mapped_column(String(255))
    ip_address: Mapped[str | None] = mapped_column(String(45))

    staff_user: Mapped[StaffUserORM] = relationship()


class StockMovementORM(Base):
    """Livro-razão de estoque.

    As linhas são escritas pelo trigger ``trg_products_stock_ledger`` sempre que
    ``products.stock`` muda. A aplicação não insere aqui diretamente: ela define
    o contexto com ``app.stock_service.set_stock_context`` e altera o estoque.
    """

    __tablename__ = "stock_movements"
    __table_args__ = (
        CheckConstraint(
            "movement_type IN ('INITIAL_STOCK', 'RESTOCK', 'MANUAL_SALE', 'LOSS', "
            "'ADJUSTMENT', 'MARKETPLACE_SALE', 'UNTRACKED')",
            name="ck_stock_movements_type",
        ),
        CheckConstraint(
            "source IN ('PAINEL', 'MARKETPLACE', 'BANCO')",
            name="ck_stock_movements_source",
        ),
        CheckConstraint(
            "quantity > 0 AND quantity = abs(delta)",
            name="ck_stock_movements_quantity",
        ),
        CheckConstraint(
            "stock_after = stock_before + delta",
            name="ck_stock_movements_balance",
        ),
        Index(
            "ix_stock_movements_product_id_created_at",
            "product_id",
            "created_at",
        ),
        {
            "comment": (
                "Livro-razão de estoque: uma linha por alteração de "
                "products.stock. Preenchido pelo trigger trg_products_stock_ledger."
            )
        },
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    product_id: Mapped[str] = mapped_column(
        String(50),
        ForeignKey("products.id", name="fk_stock_movements_product_id"),
        nullable=False,
    )
    product_name: Mapped[str] = mapped_column(String(120), nullable=False)
    movement_type: Mapped[str] = mapped_column(String(30), nullable=False)
    source: Mapped[str] = mapped_column(String(20), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    delta: Mapped[int] = mapped_column(Integer, nullable=False)
    stock_before: Mapped[int] = mapped_column(Integer, nullable=False)
    stock_after: Mapped[int] = mapped_column(Integer, nullable=False)
    reserved_stock_after: Mapped[int] = mapped_column(Integer, nullable=False)
    order_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey(
            "orders.id",
            ondelete="SET NULL",
            name="fk_stock_movements_order_id",
        ),
        index=True,
    )
    staff_user_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("staff_users.id", name="fk_stock_movements_staff_user_id"),
        index=True,
    )
    buyer_name: Mapped[str | None] = mapped_column(String(80))
    buyer_email: Mapped[str | None] = mapped_column(String(254))
    unit_price: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    note: Mapped[str | None] = mapped_column(String(500))
    db_user: Mapped[str] = mapped_column(
        String(63),
        nullable=False,
        server_default=text("SESSION_USER"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        index=True,
    )

    staff_user: Mapped[StaffUserORM | None] = relationship()
