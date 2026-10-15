from datetime import datetime
from decimal import Decimal
from enum import StrEnum
import re
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator


EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


def normalize_payer_email(value: str) -> str:
    normalized = value.strip().lower()
    if len(normalized) > 254 or not EMAIL_PATTERN.fullmatch(normalized):
        raise ValueError("Informe um e-mail válido com no máximo 254 caracteres")
    return normalized


class OrderStatus(StrEnum):
    PENDING = "PENDING"
    PAID = "PAID"
    CANCELLED = "CANCELLED"


class PricingAudience(StrEnum):
    MEMBER = "MEMBER"
    NON_MEMBER = "NON_MEMBER"


class ReservationStatus(StrEnum):
    ACTIVE = "ACTIVE"
    RELEASED = "RELEASED"
    CONSUMED = "CONSUMED"


class PaymentStatus(StrEnum):
    CREATING = "CREATING"
    WAITING_PAYMENT = "WAITING_PAYMENT"
    APPROVED = "APPROVED"
    EXPIRED = "EXPIRED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class PaymentProvider(StrEnum):
    MOCK = "MOCK"
    MERCADOPAGO = "MERCADOPAGO"


class ManualReviewReason(StrEnum):
    AMOUNT_MISMATCH = "AMOUNT_MISMATCH"
    MISSING_APPROVED_AT = "MISSING_APPROVED_AT"
    LATE_PAYMENT_STOCK_CONFLICT = "LATE_PAYMENT_STOCK_CONFLICT"
    RESERVATION_NOT_ACTIVE = "RESERVATION_NOT_ACTIVE"
    PAYMENT_AFTER_CANCELLATION = "PAYMENT_AFTER_CANCELLATION"
    RESERVATION_RELEASE_FAILED = "RESERVATION_RELEASE_FAILED"


class Product(BaseModel):
    id: str
    name: str
    stock: int
    category: str


class OrderItemInput(BaseModel):
    product_id: str
    quantity: int = Field(ge=1, le=20)


class OrderCreate(BaseModel):
    buyer_name: str = Field(min_length=2, max_length=80)
    payer_email: str = Field(min_length=3, max_length=254)
    pricing_audience: PricingAudience
    items: list[OrderItemInput] = Field(min_length=1)

    @field_validator("buyer_name")
    @classmethod
    def normalize_buyer_name(cls, value: str) -> str:
        value = value.strip()
        if len(value) < 2:
            raise ValueError("Informe um nome com pelo menos 2 caracteres")
        return value

    @field_validator("payer_email")
    @classmethod
    def validate_payer_email(cls, value: str) -> str:
        return normalize_payer_email(value)


class OrderItem(BaseModel):
    product_id: str
    product_name: str
    quantity: int
    unit_price: float
    subtotal: float


class PaymentData(BaseModel):
    txid: str
    qr_code: str
    copy_paste: str
    status: PaymentStatus
    expires_at: datetime | None = None
    simulated: bool = True


class Order(BaseModel):
    id: UUID
    buyer_name: str
    items: list[OrderItem]
    total: float
    status: OrderStatus
    pricing_audience: PricingAudience
    price_tier_id: int
    created_at: datetime
    paid_at: datetime | None = None
    payment: PaymentData


class PriceTier(BaseModel):
    id: int
    pricing_audience: PricingAudience
    min_quantity: int
    max_quantity: int | None
    unit_price: float


class MercadoPagoWebhookData(BaseModel):
    id: str | int


class MercadoPagoWebhookPayload(BaseModel):
    id: str | int
    type: str = Field(default="order", max_length=120)
    action: str = Field(default="order.updated", max_length=120)
    data: MercadoPagoWebhookData


class ManualReview(BaseModel):
    id: UUID
    order_id: UUID
    buyer_name: str
    reason: ManualReviewReason
    details: str | None
    created_at: datetime
    resolved_at: datetime | None


# ---------------------------------------------------------------------------
# Controle de estoque (painel /estoque)
# ---------------------------------------------------------------------------

USERNAME_PATTERN = re.compile(r"^[a-z0-9._-]{3,50}$")
# Caracteres de controle (inclusive NUL, que o PostgreSQL não aceita em texto).
# Quebra de linha e tab são permitidas apenas na observação.
CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
LINE_BREAKS = re.compile(r"[\r\n\t]")


class StaffRole(StrEnum):
    ADMIN = "ADMIN"
    OPERADOR = "OPERADOR"


class StockMovementType(StrEnum):
    INITIAL_STOCK = "INITIAL_STOCK"
    RESTOCK = "RESTOCK"
    MANUAL_SALE = "MANUAL_SALE"
    LOSS = "LOSS"
    ADJUSTMENT = "ADJUSTMENT"
    MARKETPLACE_SALE = "MARKETPLACE_SALE"
    UNTRACKED = "UNTRACKED"


class StockMovementSource(StrEnum):
    PAINEL = "PAINEL"
    MARKETPLACE = "MARKETPLACE"
    BANCO = "BANCO"


# Tipos que um operador pode lançar pelo painel.
PANEL_MOVEMENT_TYPES = {
    StockMovementType.RESTOCK,
    StockMovementType.MANUAL_SALE,
    StockMovementType.LOSS,
    StockMovementType.ADJUSTMENT,
}


def normalize_username(value: str) -> str:
    normalized = value.strip().lower()
    if not USERNAME_PATTERN.fullmatch(normalized):
        raise ValueError(
            "Usuário deve ter de 3 a 50 caracteres: letras minúsculas, números, '.', '_' ou '-'"
        )
    return normalized


class StaffLogin(BaseModel):
    username: str = Field(min_length=1, max_length=50)
    password: str = Field(min_length=1, max_length=200)


class StaffUser(BaseModel):
    id: int
    username: str
    full_name: str
    role: StaffRole


class StockProduct(BaseModel):
    id: str
    name: str
    category: str
    stock: int
    reserved_stock: int
    available: int
    out_today: int
    last_movement_at: datetime | None = None


class StockMovementCreate(BaseModel):
    product_id: str = Field(min_length=1, max_length=50)
    movement_type: StockMovementType
    quantity: int | None = Field(default=None, ge=1, le=500)
    counted_stock: int | None = Field(default=None, ge=0, le=10000)
    buyer_name: str | None = Field(default=None, max_length=80)
    buyer_email: str | None = Field(default=None, max_length=254)
    note: str | None = Field(default=None, max_length=500)

    @field_validator("buyer_name", "note")
    @classmethod
    def strip_optional_text(cls, value: str | None, info) -> str | None:
        if value is None:
            return None
        if CONTROL_CHARS.search(value) or (
            info.field_name == "buyer_name" and LINE_BREAKS.search(value)
        ):
            raise ValueError("Remova caracteres especiais/invisíveis do texto")
        value = value.strip()
        return value or None

    @field_validator("buyer_email")
    @classmethod
    def validate_optional_email(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        return normalize_payer_email(value)

    @model_validator(mode="after")
    def validate_by_type(self) -> "StockMovementCreate":
        if self.movement_type not in PANEL_MOVEMENT_TYPES:
            raise ValueError("Tipo de movimentação não pode ser lançado pelo painel")
        if self.movement_type == StockMovementType.ADJUSTMENT:
            if self.counted_stock is None:
                raise ValueError("Informe a contagem física para o ajuste de inventário")
            if not self.note:
                raise ValueError("Explique o motivo do ajuste de inventário")
        elif self.quantity is None:
            raise ValueError("Informe a quantidade")
        if self.movement_type == StockMovementType.MANUAL_SALE:
            if not self.buyer_name or len(self.buyer_name) < 2:
                raise ValueError("Informe o nome de quem comprou")
        return self


class StockMovement(BaseModel):
    id: int
    product_id: str
    product_name: str
    movement_type: StockMovementType
    source: StockMovementSource
    quantity: int
    delta: int
    stock_before: int
    stock_after: int
    order_id: UUID | None = None
    buyer_name: str | None = None
    buyer_email: str | None = None
    unit_price: float | None = None
    note: str | None = None
    staff_username: str | None = None
    staff_name: str | None = None
    created_at: datetime


class StockMovementResult(BaseModel):
    movement: StockMovement
    product: StockProduct


class StockSummary(BaseModel):
    day: str
    units_out_today: int
    units_in_today: int
    marketplace_sales_today: int
    manual_sales_today: int
    losses_today: int
    total_stock: int
    total_reserved: int
    total_available: int


class PriceTable(BaseModel):
    """Faixas de preço ativas de um público, como exibidas no painel."""

    pricing_audience: PricingAudience
    base_price: float | None = None
    tiers: list[PriceTier]


class PriceTableUpdate(BaseModel):
    """Novo preço base (1 lata); para membros, as demais faixas são derivadas dele."""

    pricing_audience: PricingAudience
    base_price: Decimal = Field(gt=Decimal("0"), le=Decimal("1000.00"), decimal_places=2)
