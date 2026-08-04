import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy.ext.asyncio import AsyncSession

from .config import settings
from .db.base import get_db
from .models import (
    Order,
    OrderCreate,
    MercadoPagoWebhookPayload,
    PriceTier,
    PricingAudience,
    Product,
    WebhookPayload,
)
from .payments import (
    PaymentProviderError,
    create_payment_provider,
    verify_mercadopago_signature,
)
from .payment_service import (
    PaymentServiceError,
    create_order_with_payment,
    process_mercadopago_payment,
)
from .store import store

BASE_DIR = Path(__file__).resolve().parent
WEBHOOK_TOKEN = "mvp-local-token"
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    payment_provider = create_payment_provider(settings)
    app.state.payment_provider = payment_provider
    app.state.payment_settings = settings
    try:
        yield
    finally:
        await payment_provider.aclose()


app = FastAPI(title="Red Bull PDV — MVP", version="0.1.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")


@app.get("/", include_in_schema=False)
def frontend() -> FileResponse:
    return FileResponse(BASE_DIR / "static" / "index.html")


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "storage": "postgresql", "payment": "simulated"}


@app.get("/api/products", response_model=list[Product])
async def products(db: AsyncSession = Depends(get_db)) -> list[Product]:
    return await store.list_products(db)


@app.get("/api/price-tiers", response_model=list[PriceTier])
async def price_tiers(
    pricing_audience: PricingAudience,
    db: AsyncSession = Depends(get_db),
) -> list[PriceTier]:
    return await store.list_price_tiers(db, pricing_audience)


@app.post("/api/orders", response_model=Order, status_code=201)
async def create_order(
    payload: OrderCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> Order:
    try:
        return await create_order_with_payment(
            db,
            payload,
            request.app.state.payment_provider,
        )
    except PaymentServiceError as exc:
        raise HTTPException(502, str(exc)) from exc


@app.get("/api/orders/{order_id}", response_model=Order)
async def get_order(order_id: str, db: AsyncSession = Depends(get_db)) -> Order:
    return await store.get_order(db, order_id)


@app.post("/api/webhooks/pix")
async def pix_webhook(
    payload: WebhookPayload,
    x_webhook_token: str | None = Header(None),
    db: AsyncSession = Depends(get_db),
) -> dict:
    if x_webhook_token != WEBHOOK_TOKEN:
        raise HTTPException(401, "Assinatura do webhook inválida")
    if payload.event != "PIX_PAID":
        return {"received": True, "processed": False, "reason": "Evento ignorado"}
    order, processed = await store.confirm_payment(db, payload.txid)
    return {"received": True, "processed": processed, "order_id": str(order.id), "status": order.status}


@app.post("/api/webhooks/mercadopago")
async def mercadopago_webhook(
    payload: MercadoPagoWebhookPayload,
    request: Request,
    x_signature: str | None = Header(None),
    x_request_id: str | None = Header(None),
    db: AsyncSession = Depends(get_db),
) -> dict:
    payment_id = str(payload.data.id)
    webhook_secret = request.app.state.payment_settings.mercadopago_webhook_secret
    if not verify_mercadopago_signature(
        x_signature,
        x_request_id,
        payment_id,
        webhook_secret,
    ):
        raise HTTPException(401, "Assinatura do webhook inválida")

    try:
        provider_payment = await request.app.state.payment_provider.get_payment(
            payment_id
        )
    except PaymentProviderError as exc:
        logger.warning(
            "Falha ao consultar pagamento Mercado Pago payment_id=%s: %s",
            payment_id,
            exc,
        )
        raise HTTPException(503, "Não foi possível confirmar o pagamento") from exc

    result = await process_mercadopago_payment(
        db,
        event_id=str(payload.id),
        event_type=payload.action or payload.type,
        webhook_payment_id=payment_id,
        provider_payment=provider_payment,
    )
    if result.outcome in {"already_paid", "not_approved", "unknown_payment"}:
        logger.info(
            "Webhook Mercado Pago reconhecido: outcome=%s payment_id=%s order_id=%s",
            result.outcome,
            payment_id,
            result.order_id,
        )
    elif result.outcome != "approved":
        logger.warning(
            "Webhook Mercado Pago não processado: outcome=%s payment_id=%s order_id=%s",
            result.outcome,
            payment_id,
            result.order_id,
        )
    return {
        "received": True,
        "processed": result.processed,
        "outcome": result.outcome,
        "order_id": str(result.order_id) if result.order_id else None,
    }


@app.post("/api/mock/pay/{txid}")
async def mock_payment(txid: str, db: AsyncSession = Depends(get_db)) -> dict:
    """Somente MVP: simula o gateway chamando o webhook válido."""
    order, processed = await store.confirm_payment(db, txid)
    return {"processed": processed, "order_id": str(order.id), "status": order.status}


@app.get("/api/mock/qr/{txid}")
def mock_qr(txid: str) -> Response:
    safe = txid.replace("<", "").replace(">", "")
    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="240" height="240" viewBox="0 0 240 240"><rect width="240" height="240" rx="18" fill="white"/><g fill="#071d49"><rect x="24" y="24" width="64" height="64"/><rect x="152" y="24" width="64" height="64"/><rect x="24" y="152" width="64" height="64"/><path d="M110 24h20v42h-20zM108 86h52v20h-32v22h-20zM96 142h24v22h20v52H98v-22h20v-30H96zM154 128h62v20h-42v22h42v46h-22v-24h-42v-42h22z"/></g><text x="120" y="122" text-anchor="middle" font-family="sans-serif" font-size="10" fill="#e21b2d">PIX MVP</text><title>{safe}</title></svg>'''
    return Response(svg, media_type="image/svg+xml")

