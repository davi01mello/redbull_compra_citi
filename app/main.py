import hashlib
import io
import json
from pathlib import Path

import qrcode
from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .config import settings
from .database import create_repository
from .models import Order, OrderCreate, Product
from .payments import PaymentProviderError, create_payment_provider, verify_openpix_signature

BASE_DIR = Path(__file__).resolve().parent
repository = create_repository(settings.database_url)
payment_provider = create_payment_provider(settings)

app = FastAPI(title="Red Bull PDV — CITi", version="0.2.0")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")


@app.get("/", include_in_schema=False)
def frontend() -> FileResponse:
    return FileResponse(BASE_DIR / "static" / "index.html")


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "storage": "database", "payment": settings.payment_provider}


@app.get("/api/products", response_model=list[Product])
def products() -> list[Product]:
    return repository.list_products()


@app.post("/api/orders", response_model=Order, status_code=201)
def create_order(payload: OrderCreate) -> Order:
    reserved = repository.reserve_order(payload)
    try:
        charge = payment_provider.create_charge(reserved.correlation_id, reserved.total_cents)
        return repository.attach_payment(reserved.id, charge.id, charge.br_code, charge.expires_at, charge.simulated)
    except PaymentProviderError as exc:
        repository.fail_order(reserved.id)
        raise HTTPException(502, str(exc)) from exc
    except Exception:
        repository.fail_order(reserved.id)
        raise


@app.get("/api/orders/{order_id}", response_model=Order)
def get_order(order_id: str) -> Order:
    return repository.get_order(order_id)


@app.get("/api/orders/{order_id}/qr", include_in_schema=False)
def order_qr(order_id: str) -> Response:
    br_code = repository.get_br_code(order_id)
    image = qrcode.make(br_code)
    output = io.BytesIO()
    image.save(output, format="PNG")
    return Response(output.getvalue(), media_type="image/png", headers={"Cache-Control": "private, max-age=300"})


@app.post("/api/webhooks/openpix")
async def openpix_webhook(request: Request, x_openpix_signature: str | None = Header(None)) -> dict:
    raw_body = await request.body()
    # A Woovi exige HTTP 200 em um payload de teste sem cobrança ao cadastrar a URL.
    try:
        test_payload = json.loads(raw_body)
    except json.JSONDecodeError:
        test_payload = None
    if isinstance(test_payload, dict) and test_payload.get("data_criacao") and not test_payload.get("charge") and not test_payload.get("pix"):
        return {}
    if not verify_openpix_signature(raw_body, x_openpix_signature, settings.openpix_webhook_secret):
        raise HTTPException(401, "Assinatura do webhook inválida")
    if not isinstance(test_payload, dict):
        raise HTTPException(400, "Payload inválido")
    payload = test_payload

    event_type = payload.get("event", "")
    charge = payload.get("charge") or payload.get("pix", {}).get("charge") or {}
    correlation_id = charge.get("correlationID")
    transaction = payload.get("pix", {})
    event_id = transaction.get("endToEndId") or charge.get("transactionID") or hashlib.sha256(raw_body).hexdigest()

    if event_type == "OPENPIX:CHARGE_EXPIRED":
        if not correlation_id:
            raise HTTPException(400, "Cobrança sem correlationID")
        processed = repository.expire_order(correlation_id)
        return {"received": True, "processed": processed, "reason": "Cobrança expirada"}
    if event_type != "OPENPIX:CHARGE_COMPLETED":
        return {"received": True, "processed": False, "reason": "Evento ignorado"}
    if not correlation_id:
        raise HTTPException(400, "Cobrança sem correlationID")

    try:
        confirmed = payment_provider.get_charge(correlation_id)
    except PaymentProviderError as exc:
        # Um 5xx faz a Woovi tentar o webhook novamente sem confirmar um pagamento incerto.
        raise HTTPException(503, str(exc)) from exc
    if not confirmed.completed:
        raise HTTPException(409, "Cobrança ainda não foi confirmada pela Woovi")
    order, processed = repository.confirm_payment(correlation_id, event_id, event_type, confirmed.value_cents)
    return {"received": True, "processed": processed, "order_id": str(order.id), "status": order.status}


@app.post("/api/mock/pay/{txid}")
def mock_payment(txid: str) -> dict:
    if settings.payment_provider != "mock":
        raise HTTPException(404, "Rota disponível apenas no modo de demonstração")
    order = repository.get_correlation(txid)
    if not order:
        raise HTTPException(404, "Transação não encontrada")
    confirmed, processed = repository.confirm_payment(txid, f"mock-{txid}", "MOCK:CHARGE_COMPLETED", round(order.total * 100))
    return {"processed": processed, "order_id": str(confirmed.id), "status": confirmed.status}

