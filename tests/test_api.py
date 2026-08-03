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


@pytest.mark.parametrize(
    ("quantity", "unit_price", "total"),
    [(1, 8.5, 8.5), (2, 8.0, 16.0), (3, 8.0, 24.0), (4, 7.5, 30.0), (5, 7.5, 37.5)],
)
def test_order_uses_progressive_price(client, quantity, unit_price, total):
    response = client.post(
        "/api/orders",
        json={"buyer_name": "Felipe", "items": [{"product_id": "original", "quantity": quantity}]},
    )

    assert response.status_code == 201
    order = response.json()
    assert order["items"][0]["unit_price"] == unit_price
    assert order["total"] == total


def test_progressive_price_uses_total_quantity_across_flavors(client):
    response = client.post(
        "/api/orders",
        json={
            "buyer_name": "Felipe",
            "items": [
                {"product_id": "original", "quantity": 2},
                {"product_id": "tropical", "quantity": 2},
            ],
        },
    )

    assert response.status_code == 201
    order = response.json()
    assert all(item["unit_price"] == 7.5 for item in order["items"])
    assert order["total"] == 30.0


def test_webhook_rejects_invalid_token(client):
    response = client.post("/api/webhooks/pix", json={"txid": "invalid"})
    assert response.status_code == 401
