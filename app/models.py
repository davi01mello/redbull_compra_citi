from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, Field


class OrderStatus(StrEnum):
    PENDING = "PENDING"
    PAID = "PAID"
    CANCELLED = "CANCELLED"


class Product(BaseModel):
    id: str
    name: str
    description: str
    color: str
    price: float
    stock: int


class OrderItemInput(BaseModel):
    product_id: str
    quantity: int = Field(ge=1, le=20)


class OrderCreate(BaseModel):
    buyer_name: str = Field(min_length=2, max_length=80)
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
    simulated: bool = True


class Order(BaseModel):
    id: UUID
    buyer_name: str
    items: list[OrderItem]
    total: float
    status: OrderStatus
    created_at: datetime
    paid_at: datetime | None = None
    payment: PaymentData


class WebhookPayload(BaseModel):
    txid: str
    event: str = "PIX_PAID"

