import asyncio
import hashlib
import hmac
import json
from datetime import UTC, datetime
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
    test_payer_first_name: str | None = None,
    test_payer_email: str | None = None,
) -> Settings:
    return Settings(
        mercadopago_access_token=access_token,
        mercadopago_webhook_secret=webhook_secret,
        payment_provider="mercadopago",
        charge_expires_in=1800,
        mercadopago_test_payer_first_name=test_payer_first_name,
        mercadopago_test_payer_email=test_payer_email,
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
                            "payment_method": {
                                "qr_code": "PIX-COPIA-E-COLA",
                                "ticket_url": "https://example.test/pix/order-123",
                            },
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
    assert result.qr_code == "https://example.test/pix/order-123"
    assert result.copy_paste == "PIX-COPIA-E-COLA"
    assert result.amount == Decimal("16.00")
    remaining = (result.expires_at - datetime.now(UTC)).total_seconds()
    assert 590 <= remaining <= 600


def test_create_charge_includes_test_payer_fields_only_when_configured():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content)
        return httpx.Response(
            201,
            json={
                "id": "order-apro",
                "transactions": {
                    "payments": [
                        {
                            "id": "payment-apro",
                            "payment_method": {
                                "qr_code": "PIX-APRO",
                                "ticket_url": "https://example.test/pix/apro",
                            },
                        }
                    ]
                },
            },
        )

    async def create(provider):
        return await provider.create_charge(
            external_reference="order-test-apro",
            amount=Decimal("8.50"),
            idempotency_key="charge-test-apro",
        )

    async def scenario():
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="https://api.mercadopago.com",
        )
        provider = MercadoPagoProvider(
            mercadopago_settings(
                test_payer_first_name="APRO",
                test_payer_email="test_user_br@testuser.com",
            ),
            client=client,
        )
        try:
            await create(provider)
        finally:
            await provider.aclose()

    asyncio.run(scenario())
    assert captured["payload"]["payer"] == {
        "email": "test_user_br@testuser.com",
        "first_name": "APRO",
    }


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


def test_cancel_order_sends_stable_idempotency_key():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["request"] = request
        return httpx.Response(200, json={"id": "ORD123", "status": "cancelled"})

    async def cancel(provider):
        await provider.cancel_order("ORD123", "redbull-order-local-cancel")

    run_with_provider(handler, cancel)
    request = captured["request"]
    assert request.method == "POST"
    assert request.url.path == "/v1/orders/ORD123/cancel"
    assert request.headers["Authorization"] == "Bearer TEST-ACCESS-TOKEN"
    assert request.headers["X-Idempotency-Key"] == (
        "redbull-order-local-cancel"
    )


def test_cancel_order_accepts_already_cancelled_response_as_idempotent():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            409,
            json={
                "errors": [{"code": "order_already_canceled"}],
                "message": "Order already canceled",
            },
        )

    async def cancel(provider):
        await provider.cancel_order("ORD123", "cancel-key")

    run_with_provider(handler, cancel)


def test_cancel_order_reports_provider_error():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            409,
            json={"code": "cannot_cancel_order", "message": "already paid"},
        )

    async def cancel(provider):
        await provider.cancel_order("ORD123", "cancel-key")

    with pytest.raises(PaymentProviderError, match="already paid"):
        run_with_provider(handler, cancel)


def test_create_charge_rejects_response_without_pix_code():
    def handler(_request: httpx.Request) -> httpx.Response:
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
        return await provider.create_charge("order-1", Decimal("8.50"), "key-1")

    with pytest.raises(PaymentProviderError, match="não retornou a URL do QR Code"):
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


def test_factory_does_not_fallback_to_mock_when_provider_is_missing(monkeypatch):
    monkeypatch.delenv("PAYMENT_PROVIDER", raising=False)
    settings = Settings.from_env()

    assert settings.payment_provider == ""
    with pytest.raises(RuntimeError, match="PAYMENT_PROVIDER"):
        create_payment_provider(settings)


def test_get_order_queries_by_order_id_and_maps_accredited_payment():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["request"] = request
        return httpx.Response(
            200,
            json={
                "id": "ORDTST123",
                "status": "processed",
                "status_detail": "accredited",
                "total_amount": "16.00",
                "total_paid_amount": "16.00",
                "currency": "BRL",
                "last_updated_date": "2026-08-04T20:00:00Z",
                "transactions": {
                    "payments": [
                        {
                            "id": "PAY123",
                            "amount": "16.00",
                            "paid_amount": "16.00",
                            "status": "processed",
                            "status_detail": "accredited",
                        }
                    ]
                },
            },
        )

    async def get_order(provider):
        return await provider.get_order("ORDTST123")

    result = run_with_provider(handler, get_order)
    assert captured["request"].url.path == "/v1/orders/ORDTST123"
    assert captured["request"].headers["Authorization"] == (
        "Bearer TEST-ACCESS-TOKEN"
    )
    assert result.provider_order_id == "ORDTST123"
    assert result.provider_payment_id == "PAY123"
    assert result.status == "APPROVED"
    assert result.amount == Decimal("16")
    assert result.approved_at == datetime(2026, 8, 4, 20, 0, tzinfo=UTC)


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
