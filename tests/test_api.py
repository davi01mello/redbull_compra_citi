import asyncio

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import delete, select, update

from app.db.base import AsyncSessionLocal, engine
from app.db.models import OrderORM, PriceTierORM, ProductORM
from app.models import (
    OrderCreate,
    OrderItemInput,
    PricingAudience,
    ReservationStatus,
)
from app.main import app
from app.store import store


INITIAL_STOCK = {"original": 24, "tropical": 18, "melancia": 16}


async def reset_database():
    async with AsyncSessionLocal() as session, session.begin():
        await session.execute(delete(OrderORM))
        await session.execute(update(PriceTierORM).values(active=True))
        for product_id, stock in INITIAL_STOCK.items():
            await session.execute(
                update(ProductORM)
                .where(ProductORM.id == product_id)
                .values(stock=stock, reserved_stock=0)
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
    products = client.get("/api/products").json()
    assert len(products) == 3
    assert all(product["category"] == "Regular" for product in products)
    assert all("price" not in product for product in products)
    member_tiers = client.get(
        "/api/price-tiers", params={"pricing_audience": "MEMBER"}
    ).json()
    assert [tier["unit_price"] for tier in member_tiers] == [8.5, 8.0, 7.5]


def test_product_listing_shows_stock_minus_active_reservations(client):
    created = client.post(
        "/api/orders",
        json={
            "buyer_name": "Reserva visível",
            "pricing_audience": "MEMBER",
            "items": [{"product_id": "original", "quantity": 2}],
        },
    )
    assert created.status_code == 201

    products = client.get("/api/products").json()
    original = next(product for product in products if product["id"] == "original")
    assert original["stock"] == INITIAL_STOCK["original"] - 2


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

    refreshed = client.get(f"/api/orders/{order['id']}").json()
    assert refreshed["status"] == "PAID"
    assert refreshed["payment"]["status"] == "APPROVED"


def test_order_cancellation_releases_reservation_and_is_idempotent(client):
    created = client.post(
        "/api/orders",
        json={
            "buyer_name": "Cancelamento",
            "pricing_audience": "MEMBER",
            "items": [{"product_id": "original", "quantity": 2}],
        },
    )
    assert created.status_code == 201
    order = created.json()

    during_reservation = next(
        product
        for product in client.get("/api/products").json()
        if product["id"] == "original"
    )
    assert during_reservation["stock"] == INITIAL_STOCK["original"] - 2

    first = client.post(f"/api/orders/{order['id']}/cancel")
    second = client.post(f"/api/orders/{order['id']}/cancel")
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["status"] == "CANCELLED"
    assert first.json()["payment"]["status"] == "CANCELLED"
    assert second.json()["status"] == "CANCELLED"

    after_cancellation = next(
        product
        for product in client.get("/api/products").json()
        if product["id"] == "original"
    )
    assert after_cancellation["stock"] == INITIAL_STOCK["original"]

    simulated_payment = client.post(f"/api/mock/pay/{order['payment']['txid']}")
    assert simulated_payment.status_code == 409


def test_paid_order_cannot_be_cancelled(client):
    created = client.post(
        "/api/orders",
        json={
            "buyer_name": "Já pago",
            "pricing_audience": "MEMBER",
            "items": [{"product_id": "original", "quantity": 1}],
        },
    ).json()
    paid = client.post(f"/api/mock/pay/{created['payment']['txid']}")
    assert paid.status_code == 200

    cancellation = client.post(f"/api/orders/{created['id']}/cancel")
    assert cancellation.status_code == 409
    assert "já foi confirmado" in cancellation.json()["detail"]


def test_order_exposes_locally_generated_pix_qr(client):
    created = client.post(
        "/api/orders",
        json={
            "buyer_name": "QR local",
            "pricing_audience": "MEMBER",
            "items": [{"product_id": "original", "quantity": 1}],
        },
    )
    assert created.status_code == 201
    order = created.json()
    assert order["payment"]["qr_code"] == f"/api/orders/{order['id']}/qr"
    assert order["payment"]["status"] == "WAITING_PAYMENT"
    assert order["payment"]["expires_at"] is not None

    qr = client.get(order["payment"]["qr_code"])
    assert qr.status_code == 200
    assert qr.headers["content-type"].startswith("image/svg+xml")
    assert qr.headers["cache-control"] == "no-store"
    assert b"<svg" in qr.content


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


def test_concurrent_orders_compete_for_last_available_unit():
    async def scenario():
        await reset_database()
        async with AsyncSessionLocal() as session, session.begin():
            await session.execute(
                update(ProductORM)
                .where(ProductORM.id == "original")
                .values(stock=1, reserved_stock=0)
            )

        async def attempt_order(buyer_name: str) -> int:
            async with AsyncSessionLocal() as session:
                try:
                    await store.reserve_order(
                        session,
                        OrderCreate(
                            buyer_name=buyer_name,
                            pricing_audience=PricingAudience.MEMBER,
                            items=[OrderItemInput(product_id="original", quantity=1)],
                        ),
                    )
                except HTTPException as exc:
                    return exc.status_code
                return 201

        statuses = await asyncio.gather(
            attempt_order("Concorrente A"),
            attempt_order("Concorrente B"),
        )

        async with AsyncSessionLocal() as session:
            product = (
                await session.execute(
                    select(ProductORM).where(ProductORM.id == "original")
                )
            ).scalar_one()
            orders = list((await session.execute(select(OrderORM))).scalars())
            listed_products = await store.list_products(session)
            listed_original = next(
                product for product in listed_products if product.id == "original"
            )
            result = (
                statuses,
                product.stock,
                product.reserved_stock,
                [order.reservation_status for order in orders],
                listed_original.stock,
            )
        await engine.dispose()
        return result

    statuses, stock, reserved_stock, reservation_statuses, available_stock = (
        asyncio.run(scenario())
    )
    assert sorted(statuses) == [201, 409]
    assert stock == 1
    assert reserved_stock == 1
    assert reservation_statuses == [ReservationStatus.ACTIVE]
    assert available_stock == 0


def test_failure_before_reservation_does_not_create_partial_reservation():
    async def scenario():
        await reset_database()
        async with AsyncSessionLocal() as session, session.begin():
            await session.execute(
                update(ProductORM)
                .where(ProductORM.id == "original")
                .values(stock=2, reserved_stock=0)
            )
            await session.execute(
                update(ProductORM)
                .where(ProductORM.id == "tropical")
                .values(stock=0, reserved_stock=0)
            )

        async with AsyncSessionLocal() as session:
            with pytest.raises(HTTPException) as exc_info:
                await store.reserve_order(
                    session,
                    OrderCreate(
                        buyer_name="Sem estoque",
                        pricing_audience=PricingAudience.MEMBER,
                        items=[
                            OrderItemInput(product_id="original", quantity=1),
                            OrderItemInput(product_id="tropical", quantity=1),
                        ],
                    ),
                )

        async with AsyncSessionLocal() as session:
            products = list(
                (
                    await session.execute(
                        select(ProductORM)
                        .where(ProductORM.id.in_(["original", "tropical"]))
                        .order_by(ProductORM.id)
                    )
                ).scalars()
            )
            order_count = len(
                list((await session.execute(select(OrderORM))).scalars())
            )
            result = (
                exc_info.value.status_code,
                [
                    (product.id, product.stock, product.reserved_stock)
                    for product in products
                ],
                order_count,
            )
        await engine.dispose()
        return result

    status_code, inventory, order_count = asyncio.run(scenario())
    assert status_code == 409
    assert inventory == [("original", 2, 0), ("tropical", 0, 0)]
    assert order_count == 0


def test_release_reservation_is_idempotent_and_keeps_physical_stock():
    async def scenario():
        await reset_database()
        async with AsyncSessionLocal() as session:
            order = await store.reserve_order(
                session,
                OrderCreate(
                    buyer_name="Liberar duas vezes",
                    pricing_audience=PricingAudience.MEMBER,
                    items=[OrderItemInput(product_id="original", quantity=2)],
                ),
            )
            first_release = await store.release_reservation(session, order.id)
            second_release = await store.release_reservation(session, order.id)

        async with AsyncSessionLocal() as session:
            product = (
                await session.execute(
                    select(ProductORM).where(ProductORM.id == "original")
                )
            ).scalar_one()
            persisted_order = (
                await session.execute(
                    select(OrderORM).where(OrderORM.id == order.id)
                )
            ).scalar_one()
            result = (
                first_release,
                second_release,
                product.stock,
                product.reserved_stock,
                persisted_order.reservation_status,
            )
        await engine.dispose()
        return result

    first, second, stock, reserved_stock, reservation_status = asyncio.run(scenario())
    assert first is True
    assert second is False
    assert stock == INITIAL_STOCK["original"]
    assert reserved_stock == 0
    assert reservation_status == ReservationStatus.RELEASED


def test_legacy_pix_webhook_was_removed(client):
    response = client.post("/api/webhooks/pix", json={"txid": "invalid"})
    assert response.status_code == 404
