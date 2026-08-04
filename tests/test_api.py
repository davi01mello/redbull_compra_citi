import asyncio

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.db.base import DATABASE_URL, get_db
from app.db.models import OrderORM, ProductORM
from app.main import app


INITIAL_STOCK = {"original": 24, "tropical": 18, "melancia": 16}
test_engine = create_async_engine(DATABASE_URL, poolclass=NullPool)
TestSession = async_sessionmaker(test_engine, expire_on_commit=False)


async def get_test_db():
    async with TestSession() as session:
        yield session


async def reset_database():
    async with TestSession() as session, session.begin():
        await session.execute(delete(OrderORM))
        for product_id, stock in INITIAL_STOCK.items():
            await session.execute(
                update(ProductORM)
                .where(ProductORM.id == product_id)
                .values(stock=stock)
            )


@pytest.fixture()
def client():
    asyncio.run(reset_database())
    app.dependency_overrides[get_db] = get_test_db
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.clear()
        asyncio.run(reset_database())


def test_health_and_products(client):
    assert client.get("/api/health").json()["storage"] == "postgresql"
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


def test_order_cancel_sets_cancelled_status(client):
    created = client.post(
        "/api/orders",
        json={"buyer_name": "Felipe", "items": [{"product_id": "original", "quantity": 2}]},
    )
    assert created.status_code == 201
    order = created.json()

    cancel = client.post(f"/api/orders/{order['id']}/cancel")
    assert cancel.status_code == 200
    assert cancel.json()["status"] == "CANCELLED"

    payment = client.post(f"/api/mock/pay/{order['payment']['txid']}")
    assert payment.status_code == 409
    assert payment.json()["detail"] == "Pedido cancelado não pode ser pago"

    products = client.get("/api/products").json()
    assert next(p for p in products if p["id"] == "original")["stock"] == INITIAL_STOCK["original"]
