from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, Field


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
    pricing_audience: PricingAudience
    items: list[OrderItemInput] = Field(min_length=1)


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
