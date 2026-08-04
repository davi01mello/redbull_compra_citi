import asyncio
import hashlib
import hmac
import json
from decimal import Decimal

import httpx
import pytest

from app.config import Settings
from app.payments import (
    MERCADOPAGO_FALLBACK_PAYER_EMAIL,
    MercadoPagoProvider,
    MockPaymentProvider,
    PaymentProviderError,
    create_payment_provider,
    verify_mercadopago_signature,
)


def mercadopago_settings(
    *,
    access_token: str = "TEST-ACCESS-TOKEN",
    webhook_secret: str = "test-webhook-secret",
) -> Settings:
    return Settings(
        mercadopago_access_token=access_token,
        mercadopago_webhook_secret=webhook_secret,
        payment_provider="mercadopago",
        charge_expires_in=1800,
    )


def run_with_provider(handler, operation):
    async def scenario():
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="https://api.mercadopago.com",
        )
        provider = MercadoPagoProvider(mercadopago_settings(), client=client)
        try:
            return await operation(provider)
        finally:
            await provider.aclose()

    return asyncio.run(scenario())


def test_create_charge_sends_correct_payload_auth_and_idempotency_key():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["request"] = request
        return httpx.Response(
            201,
            json={
                "id": "order-123",
                "transactions": {
                    "payments": [
                        {
                            "id": "payment-456",
                            "payment_method": {"qr_code": "PIX-COPIA-E-COLA"},
                        }
                    ]
                },
            },
        )

    async def create(provider):
        return await provider.create_charge(
            external_reference="order-local-789",
            amount=Decimal("16.00"),
            idempotency_key="charge-order-local-789",
        )

    result = run_with_provider(handler, create)
    request = captured["request"]
    payload = json.loads(request.content)

    assert request.method == "POST"
    assert request.url.path == "/v1/orders"
    assert request.headers["Authorization"] == "Bearer TEST-ACCESS-TOKEN"
    assert request.headers["X-Idempotency-Key"] == "charge-order-local-789"
    assert payload == {
        "type": "online",
        "total_amount": "16.00",
        "external_reference": "order-local-789",
        "processing_mode": "automatic",
        "transactions": {
            "payments": [
                {
                    "amount": "16.00",
                    "payment_method": {"id": "pix", "type": "bank_transfer"},
                    "expiration_time": "PT1800S",
                }
            ]
        },
        "payer": {"email": MERCADOPAGO_FALLBACK_PAYER_EMAIL},
    }
    assert result.provider_order_id == "order-123"
    assert result.provider_payment_id == "payment-456"
    assert result.copy_paste == "PIX-COPIA-E-COLA"
    assert result.amount == Decimal("16.00")


def test_create_charge_rejects_invalid_provider_response():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(201, content=b"not-json")

    async def create(provider):
        return await provider.create_charge("order-1", Decimal("8.50"), "key-1")

    with pytest.raises(PaymentProviderError, match="Resposta inválida"):
        run_with_provider(handler, create)


def test_create_charge_reports_timeout():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("simulated timeout", request=request)

    async def create(provider):
        return await provider.create_charge("order-1", Decimal("8.50"), "key-1")

    with pytest.raises(PaymentProviderError, match="Tempo esgotado"):
        run_with_provider(handler, create)


def test_create_charge_reports_provider_http_error():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"message": "invalid payer data"})

    async def create(provider):
        return await provider.create_charge("order-1", Decimal("8.50"), "key-1")

    with pytest.raises(PaymentProviderError, match="invalid payer data"):
        run_with_provider(handler, create)


def test_create_charge_rejects_response_without_pix_code():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            201,
            json={
                "id": "order-123",
                "transactions": {
                    "payments": [{"id": "payment-456", "payment_method": {}}]
                },
            },
        )

    async def create(provider):
        return await provider.create_charge("order-1", Decimal("8.50"), "key-1")

    with pytest.raises(PaymentProviderError, match="não retornou o código PIX"):
        run_with_provider(handler, create)


def test_factory_rejects_missing_mercadopago_credentials():
    settings = mercadopago_settings(access_token="", webhook_secret="")

    with pytest.raises(RuntimeError) as exc_info:
        create_payment_provider(settings)

    assert "MERCADOPAGO_ACCESS_TOKEN" in str(exc_info.value)
    assert "MERCADOPAGO_WEBHOOK_SECRET" in str(exc_info.value)


def test_factory_only_accepts_mock_and_mercadopago():
    settings = Settings(
        mercadopago_access_token="",
        mercadopago_webhook_secret="",
        payment_provider="openpix",
        charge_expires_in=1800,
    )
    with pytest.raises(RuntimeError, match="PAYMENT_PROVIDER"):
        create_payment_provider(settings)

    mock_settings = Settings(
        mercadopago_access_token="",
        mercadopago_webhook_secret="",
        payment_provider="mock",
        charge_expires_in=1800,
    )
    assert isinstance(create_payment_provider(mock_settings), MockPaymentProvider)


def test_get_payment_queries_by_payment_id():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["request"] = request
        return httpx.Response(
            200,
            json={
                "id": 456,
                "status": "approved",
                "transaction_amount": 16,
                "currency_id": "BRL",
                "date_approved": "2026-08-04T20:00:00Z",
            },
        )

    async def get_payment(provider):
        return await provider.get_payment("456")

    result = run_with_provider(handler, get_payment)
    assert captured["request"].url.path == "/v1/payments/456"
    assert captured["request"].headers["Authorization"] == (
        "Bearer TEST-ACCESS-TOKEN"
    )
    assert result.provider_payment_id == "456"
    assert result.status == "APPROVED"
    assert result.amount == Decimal("16")


def test_verify_mercadopago_signature_uses_official_manifest():
    secret = "webhook-secret"
    timestamp = "1704908010"
    request_id = "request-123"
    data_id = "ABC123"
    manifest = f"id:abc123;request-id:{request_id};ts:{timestamp};"
    digest = hmac.new(
        secret.encode(), manifest.encode(), hashlib.sha256
    ).hexdigest()
    signature = f"ts={timestamp},v1={digest}"

    assert verify_mercadopago_signature(
        signature, request_id, data_id, secret
    ) is True
    assert verify_mercadopago_signature(
        signature, request_id, data_id, "wrong-secret"
    ) is False
