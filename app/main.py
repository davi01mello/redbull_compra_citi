import asyncio
import logging
from contextlib import asynccontextmanager
from io import BytesIO
from pathlib import Path
from uuid import UUID

import qrcode
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from qrcode.image.svg import SvgPathImage
from sqlalchemy.ext.asyncio import AsyncSession

from .config import settings
from .db.base import AsyncSessionLocal, get_db
from .models import (
    ManualReview,
    Order,
    OrderCreate,
    PriceTier,
    PricingAudience,
    Product,
)
from .payments import (
    PaymentProviderError,
    create_payment_provider,
    verify_mercadopago_signature,
)
from .payment_service import (
    PaymentCancellationConflict,
    PaymentServiceError,
    cancel_order_payment,
    create_order_with_payment,
    expire_due_payments,
    list_manual_reviews,
    process_mercadopago_payment,
)
from .store import store

BASE_DIR = Path(__file__).resolve().parent
logger = logging.getLogger(__name__)


async def _expiration_loop(interval_seconds: int, payment_provider) -> None:
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            async with AsyncSessionLocal() as db:
                expired = await expire_due_payments(
                    db,
                    provider=payment_provider,
                )
            if expired:
                logger.info("Cobranças expiradas e reservas liberadas: %s", expired)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Falha na rotina periódica de expiração de cobranças")


@asynccontextmanager
async def lifespan(app: FastAPI):
    payment_provider = create_payment_provider(settings)
    app.state.payment_provider = payment_provider
    app.state.payment_settings = settings
    expiration_task = asyncio.create_task(
        _expiration_loop(
            settings.payment_expiration_check_interval,
            payment_provider,
        ),
        name="payment-expiration-loop",
    )
    try:
        yield
    finally:
        expiration_task.cancel()
        try:
            await expiration_task
        except asyncio.CancelledError:
            pass
        await payment_provider.aclose()


app = FastAPI(title="Red Bull PDV — MVP", version="0.1.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")


@app.get("/", include_in_schema=False)
def frontend() -> FileResponse:
    return FileResponse(BASE_DIR / "static" / "index.html")


@app.get("/api/health")
def health() -> dict:
    return {
        "status": "ok",
        "storage": "postgresql",
        "payment": settings.payment_provider,
    }


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


@app.post("/api/orders/{order_id}/cancel", response_model=Order)
async def cancel_order(
    order_id: UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> Order:
    try:
        return await cancel_order_payment(
            db,
            order_id,
            request.app.state.payment_provider,
        )
    except PaymentCancellationConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except PaymentServiceError as exc:
        raise HTTPException(502, str(exc)) from exc


@app.get("/api/orders/{order_id}/qr")
async def order_pix_qr(
    order_id: str,
    db: AsyncSession = Depends(get_db),
) -> Response:
    order = await store.get_order(db, order_id)
    qr = qrcode.QRCode(
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=10,
        border=4,
    )
    qr.add_data(order.payment.copy_paste)
    qr.make(fit=True)
    image = qr.make_image(image_factory=SvgPathImage)
    buffer = BytesIO()
    image.save(buffer)
    return Response(
        content=buffer.getvalue(),
        media_type="image/svg+xml",
        headers={
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@app.get("/api/manual-reviews", response_model=list[ManualReview])
async def manual_reviews(
    unresolved_only: bool = True,
    db: AsyncSession = Depends(get_db),
) -> list[ManualReview]:
    return await list_manual_reviews(db, unresolved_only)


@app.post("/api/webhooks/mercadopago")
async def mercadopago_webhook(
    request: Request,
    x_signature: str | None = Header(None),
    x_request_id: str | None = Header(None),
    db: AsyncSession = Depends(get_db),
) -> dict:
    # O Mercado Pago envia o ID da Order nos parâmetros da URL:
    # ?data.id=ORD...&type=order
    provider_order_id = request.query_params.get("data.id")

    if not provider_order_id:
        raise HTTPException(
            status_code=400,
            detail="Parâmetro data.id não informado",
        )

    webhook_secret = (
        request.app.state.payment_settings.mercadopago_webhook_secret
    )

    logger.warning(
        "Diagnóstico webhook: "
        "signature_present=%s request_id_present=%s "
        "data_id_present=%s secret_present=%s",
        bool(x_signature),
        bool(x_request_id),
        bool(provider_order_id),
        bool(webhook_secret),
    )

    # A assinatura deve ser validada usando o data.id da URL.
    if not verify_mercadopago_signature(
        x_signature,
        x_request_id,
        provider_order_id,
        webhook_secret,
    ):
        raise HTTPException(
            status_code=401,
            detail="Assinatura do webhook inválida",
        )

    # O corpo serve apenas para obter informações adicionais.
    # Ele não deve ser obrigatório para a rota funcionar.
    try:
        raw_payload = await request.json()
    except ValueError:
        raw_payload = {}

    if not isinstance(raw_payload, dict):
        raw_payload = {}

    event_type = str(
        raw_payload.get("action")
        or raw_payload.get("type")
        or request.query_params.get("type")
        or "order.updated"
    )

    event_id = str(
        raw_payload.get("id")
        or x_request_id
        or f"{event_type}:{provider_order_id}"
    )

    try:
        provider_payment = (
            await request.app.state.payment_provider.get_order(
                provider_order_id
            )
        )
    except PaymentProviderError as exc:
        logger.warning(
            "Falha ao consultar Order Mercado Pago "
            "provider_order_id=%s: %s",
            provider_order_id,
            exc,
        )
        raise HTTPException(
            status_code=503,
            detail="Não foi possível confirmar o pagamento",
        ) from exc

    result = await process_mercadopago_payment(
        db,
        event_id=event_id,
        event_type=event_type,
        webhook_order_id=provider_order_id,
        provider_payment=provider_payment,
    )

    logger.info(
        "Webhook Mercado Pago processado: "
        "outcome=%s provider_order_id=%s order_id=%s",
        result.outcome,
        provider_order_id,
        result.order_id,
    )

    return {
        "received": True,
        "processed": result.processed,
        "outcome": result.outcome,
        "order_id": (
            str(result.order_id)
            if result.order_id
            else None
        ),
    }


@app.post("/api/mock/pay/{txid}")
async def mock_payment(
    txid: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Somente MVP: simula o gateway chamando o webhook válido."""
    if request.app.state.payment_settings.payment_provider != "mock":
        raise HTTPException(404, "Rota disponível somente no modo mock")
    order, processed = await store.confirm_payment(db, txid)
    return {"processed": processed, "order_id": str(order.id), "status": order.status}


@app.get("/api/mock/qr/{txid}")
def mock_qr(txid: str, request: Request) -> Response:
    if request.app.state.payment_settings.payment_provider != "mock":
        raise HTTPException(404, "Rota disponível somente no modo mock")
    safe = txid.replace("<", "").replace(">", "")
    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="240" height="240" viewBox="0 0 240 240"><rect width="240" height="240" rx="18" fill="white"/><g fill="#071d49"><rect x="24" y="24" width="64" height="64"/><rect x="152" y="24" width="64" height="64"/><rect x="24" y="152" width="64" height="64"/><path d="M110 24h20v42h-20zM108 86h52v20h-32v22h-20zM96 142h24v22h20v52H98v-22h20v-30H96zM154 128h62v20h-42v22h42v46h-22v-24h-42v-42h22z"/></g><text x="120" y="122" text-anchor="middle" font-family="sans-serif" font-size="10" fill="#e21b2d">PIX MVP</text><title>{safe}</title></svg>'''
    return Response(svg, media_type="image/svg+xml")
