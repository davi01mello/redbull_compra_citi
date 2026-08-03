import base64
import hashlib
import hmac
import json

import pytest
import httpx
from fastapi.testclient import TestClient

from app.config import Settings
from app.database import create_repository
from app.main import app
from app.payments import ChargeStatus, MockPaymentProvider, OpenPixProvider, StaticPixProvider


class ConfirmingProvider(MockPaymentProvider):
    def __init__(self, settings: Settings, value_cents: int = 1600) -> None:
        super().__init__(settings)
        self.value_cents = value_cents

    def get_charge(self, correlation_id: str) -> ChargeStatus:
        return ChargeStatus(completed=True, value_cents=self.value_cents)


@pytest.fixture()
def client(monkeypatch):
    test_settings = Settings(
        database_url="sqlite:///:memory:", payment_provider="mock",
        openpix_webhook_secret="test-webhook-secret", charge_expires_in=600,
    )
    repository = create_repository(test_settings.database_url)
    monkeypatch.setattr("app.main.repository", repository)
    monkeypatch.setattr("app.main.settings", test_settings)
    monkeypatch.setattr("app.main.payment_provider", MockPaymentProvider(test_settings))
    return TestClient(app)


def create_order(client, quantity=2):
    return client.post("/api/orders", json={"buyer_name": "Felipe", "items": [{"product_id": "original", "quantity": quantity}]})


def test_health_and_products(client):
    assert client.get("/api/health").json()["storage"] == "database"
    assert len(client.get("/api/products").json()) == 3


def test_order_reserves_stock_and_payment_is_idempotent(client):
    created = create_order(client)
    assert created.status_code == 201
    order = created.json()
    assert order["status"] == "PENDING"
    assert order["payment"]["simulated"] is True
    products = client.get("/api/products").json()
    assert next(p for p in products if p["id"] == "original")["stock"] == 22

    first = client.post(f"/api/mock/pay/{order['payment']['txid']}").json()
    second = client.post(f"/api/mock/pay/{order['payment']['txid']}").json()
    assert first["processed"] is True
    assert second["processed"] is False
    products = client.get("/api/products").json()
    assert next(p for p in products if p["id"] == "original")["stock"] == 22


def test_qr_code_is_generated(client):
    order = create_order(client, quantity=1).json()
    response = client.get(order["payment"]["qr_code"])
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.content.startswith(b"\x89PNG")


def test_webhook_rejects_invalid_signature(client):
    response = client.post("/api/webhooks/openpix", json={"event": "OPENPIX:CHARGE_COMPLETED"})
    assert response.status_code == 401


def test_openpix_webhook_registration_probe(client):
    response = client.post("/api/webhooks/openpix", json={"data_criacao": "2026-08-03T20:00:00Z", "event": "OPENPIX:CHARGE_COMPLETED"})
    assert response.status_code == 200
    assert response.json() == {}


def test_signed_webhook_confirms_order_once(client, monkeypatch):
    created = create_order(client).json()
    txid = created["payment"]["txid"]
    monkeypatch.setattr("app.main.payment_provider", ConfirmingProvider(Settings(), value_cents=1600))
    payload = {
        "event": "OPENPIX:CHARGE_COMPLETED",
        "charge": {"correlationID": txid, "transactionID": "transaction-1"},
        "pix": {"endToEndId": "E123"},
    }
    body = json.dumps(payload, separators=(",", ":")).encode()
    signature = base64.b64encode(hmac.new(b"test-webhook-secret", body, hashlib.sha1).digest()).decode()
    headers = {"Content-Type": "application/json", "X-OpenPix-Signature": signature}
    first = client.post("/api/webhooks/openpix", content=body, headers=headers)
    second = client.post("/api/webhooks/openpix", content=body, headers=headers)
    assert first.status_code == 200 and first.json()["processed"] is True
    assert second.status_code == 200 and second.json()["processed"] is False
    assert client.get(f"/api/orders/{created['id']}").json()["status"] == "PAID"


def test_signed_expiration_releases_stock(client):
    created = create_order(client, quantity=2).json()
    txid = created["payment"]["txid"]
    assert next(p for p in client.get("/api/products").json() if p["id"] == "original")["stock"] == 22
    payload = {"event": "OPENPIX:CHARGE_EXPIRED", "charge": {"correlationID": txid, "transactionID": "expired-1"}}
    body = json.dumps(payload, separators=(",", ":")).encode()
    signature = base64.b64encode(hmac.new(b"test-webhook-secret", body, hashlib.sha1).digest()).decode()
    response = client.post("/api/webhooks/openpix", content=body, headers={"Content-Type": "application/json", "X-OpenPix-Signature": signature})
    assert response.status_code == 200 and response.json()["processed"] is True
    assert client.get(f"/api/orders/{created['id']}").json()["status"] == "EXPIRED"
    assert next(p for p in client.get("/api/products").json() if p["id"] == "original")["stock"] == 24


def test_openpix_charge_sends_no_customer_data(monkeypatch):
    captured = {}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def post(self, url, headers, json):
            captured.update(url=url, headers=headers, json=json)
            request = httpx.Request("POST", url)
            return httpx.Response(200, request=request, json={
                "charge": {
                    "globalID": "charge-1", "brCode": "000201-real-code",
                    "expiresDate": "2026-08-03T18:00:00Z",
                }
            })

    settings = Settings(payment_provider="openpix", openpix_app_id="test-app-id", openpix_webhook_secret="secret")
    monkeypatch.setattr("app.payments.httpx.Client", FakeClient)
    charge = OpenPixProvider(settings).create_charge("order-123", 1600)
    assert charge.br_code == "000201-real-code"
    assert captured["json"] == {"value": 1600, "correlationID": "order-123", "expiresIn": 600}
    assert "customer" not in captured["json"]
    assert captured["headers"]["Authorization"] == "test-app-id"


def test_static_pix_generates_real_brcode_with_order_value(client, monkeypatch):
    pix_settings = Settings(
        payment_provider="static_pix", pix_key="teste@example.com",
        pix_receiver_name="Miguel Teste", pix_receiver_city="Fortaleza",
    )
    monkeypatch.setattr("app.main.settings", pix_settings)
    monkeypatch.setattr("app.main.payment_provider", StaticPixProvider(pix_settings))
    order = create_order(client, quantity=2).json()
    code = order["payment"]["copy_paste"]
    assert order["payment"]["simulated"] is False
    assert order["payment"]["automatic_confirmation"] is False
    assert "teste@example.com" in code
    assert "540516.00" in code
    assert code.startswith("000201") and code[-8:-4] == "6304"
    assert client.get(order["payment"]["qr_code"]).status_code == 200
