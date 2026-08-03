import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.store import MemoryStore


@pytest.fixture()
def client(monkeypatch):
    fresh = MemoryStore()
    monkeypatch.setattr("app.main.store", fresh)
    return TestClient(app)


def test_health_and_products(client):
    assert client.get("/api/health").json()["storage"] == "memory"
    assert len(client.get("/api/products").json()) == 3


def test_order_payment_is_idempotent(client):
    created = client.post("/api/orders", json={"buyer_name": "Felipe", "items": [{"product_id": "original", "quantity": 2}]})
    assert created.status_code == 201
    order = created.json()
    first = client.post(f"/api/mock/pay/{order['payment']['txid']}").json()
    second = client.post(f"/api/mock/pay/{order['payment']['txid']}").json()
    assert first["processed"] is True
    assert second["processed"] is False
    products = client.get("/api/products").json()
    assert next(p for p in products if p["id"] == "original")["stock"] == 22


def test_webhook_rejects_invalid_token(client):
    response = client.post("/api/webhooks/pix", json={"txid": "invalid"})
    assert response.status_code == 401

