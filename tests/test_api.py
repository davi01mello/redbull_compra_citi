import asyncio

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, update

from app.db.base import AsyncSessionLocal, engine
from app.db.models import OrderORM, PriceTierORM, ProductORM
from app.models import PricingAudience
from app.main import app


INITIAL_STOCK = {"original": 24, "tropical": 18, "melancia": 16}


async def reset_database():
    async with AsyncSessionLocal() as session, session.begin():
        await session.execute(delete(OrderORM))
        await session.execute(update(PriceTierORM).values(active=True))
        for product_id, stock in INITIAL_STOCK.items():
            await session.execute(
                update(ProductORM)
                .where(ProductORM.id == product_id)
                .values(stock=stock)
            )
    await engine.dispose()


@pytest.fixture()
def client():
    asyncio.run(reset_database())
    with TestClient(app) as test_client:
        yield test_client
    asyncio.run(engine.dispose())


def test_health_and_products(client):
    assert client.get("/api/health").json()["storage"] == "postgresql"
    assert len(client.get("/api/products").json()) == 3
    member_tiers = client.get(
        "/api/price-tiers", params={"pricing_audience": "MEMBER"}
    ).json()
    assert [tier["unit_price"] for tier in member_tiers] == [8.5, 8.0, 7.5]


def test_order_payment_is_idempotent(client):
    created = client.post(
        "/api/orders",
        json={
            "buyer_name": "Felipe",
            "pricing_audience": "MEMBER",
            "items": [{"product_id": "original", "quantity": 2}],
        },
    )
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
        json={
            "buyer_name": "Felipe",
            "pricing_audience": "MEMBER",
            "items": [
                {
                    "product_id": "original",
                    "quantity": quantity,
                    "unit_price": 0.01,
                }
            ],
        },
    )

    assert response.status_code == 201
    order = response.json()
    assert order["items"][0]["unit_price"] == unit_price
    assert order["total"] == total
    assert order["pricing_audience"] == "MEMBER"
    assert isinstance(order["price_tier_id"], int)


@pytest.mark.parametrize(("quantity", "total"), [(1, 12.0), (5, 60.0)])
def test_order_uses_fixed_non_member_price(client, quantity, total):
    response = client.post(
        "/api/orders",
        json={
            "buyer_name": "Visitante",
            "pricing_audience": "NON_MEMBER",
            "items": [{"product_id": "original", "quantity": quantity}],
        },
    )

    assert response.status_code == 201
    order = response.json()
    assert order["items"][0]["unit_price"] == 12.0
    assert order["total"] == total
    assert order["pricing_audience"] == "NON_MEMBER"


def test_progressive_price_uses_total_quantity_across_flavors(client):
    response = client.post(
        "/api/orders",
        json={
            "buyer_name": "Felipe",
            "pricing_audience": "MEMBER",
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


def test_order_rejects_invalid_pricing_audience(client):
    response = client.post(
        "/api/orders",
        json={
            "buyer_name": "Felipe",
            "pricing_audience": "INVALID",
            "items": [{"product_id": "original", "quantity": 1}],
        },
    )

    assert response.status_code == 422


def test_order_rejects_quantity_without_active_price_tier():
    async def disable_member_tiers():
        await reset_database()
        async with AsyncSessionLocal() as session, session.begin():
            await session.execute(
                update(PriceTierORM)
                .where(PriceTierORM.pricing_audience == PricingAudience.MEMBER)
                .values(active=False)
            )
        await engine.dispose()

    asyncio.run(disable_member_tiers())
    try:
        with TestClient(app) as client:
            response = client.post(
                "/api/orders",
                json={
                    "buyer_name": "Felipe",
                    "pricing_audience": "MEMBER",
                    "items": [{"product_id": "original", "quantity": 1}],
                },
            )
        asyncio.run(engine.dispose())

        assert response.status_code == 422
        assert response.json()["detail"] == (
            "Nenhuma faixa de preço ativa cobre este pedido"
        )
    finally:
        asyncio.run(reset_database())


def test_webhook_rejects_invalid_token(client):
    response = client.post("/api/webhooks/pix", json={"txid": "invalid"})
    assert response.status_code == 401
