import asyncio
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select, text, update

from app.db.base import AsyncSessionLocal, engine
from app.db.models import (
    OrderORM,
    PriceTierORM,
    ProductORM,
    StaffSessionORM,
    StaffUserORM,
    StockMovementORM,
)
from app.main import app
from app.models import (
    PriceTableUpdate,
    StaffRole,
    StaffUser,
    StockMovementCreate,
    StockMovementType,
)
from app.price_service import update_price_table
from app.staff_auth import (
    CSRF_HEADER_VALUE,
    SESSION_COOKIE_NAME,
    hash_password,
    login_throttle,
    verify_password,
)
from app.stock_service import apply_panel_movement


INITIAL_STOCK = {"original": 24, "tropical": 18, "melancia": 16}
PANEL = {"X-Requested-With": CSRF_HEADER_VALUE}
PASSWORD = "senha-segura-123"


def run(coro):
    """Executa uma corrotina num loop novo sem reaproveitar conexões de outro loop."""
    asyncio.run(engine.dispose())
    try:
        return asyncio.run(coro)
    finally:
        asyncio.run(engine.dispose())


async def reset_stock_database() -> dict[str, int]:
    async with AsyncSessionLocal() as session, session.begin():
        await session.execute(delete(StockMovementORM))
        await session.execute(delete(StaffSessionORM))
        await session.execute(delete(StaffUserORM))
        await session.execute(delete(OrderORM))
        await session.execute(update(PriceTierORM).values(active=True))
        for product_id, stock in INITIAL_STOCK.items():
            await session.execute(
                update(ProductORM)
                .where(ProductORM.id == product_id)
                .values(stock=stock, reserved_stock=0)
            )
        # O reset acima também passa pelo trigger; começamos com o razão vazio.
        await session.execute(delete(StockMovementORM))
        ids = {}
        for username, role in (("admin", StaffRole.ADMIN), ("caixa", StaffRole.OPERADOR)):
            user = StaffUserORM(
                username=username,
                full_name=f"Usuário {username}",
                password_hash=hash_password(PASSWORD),
                role=role.value,
            )
            session.add(user)
            await session.flush()
            ids[username] = user.id
    return ids


async def fetch_movements(product_id: str | None = None) -> list[StockMovementORM]:
    async with AsyncSessionLocal() as session:
        query = select(StockMovementORM).order_by(StockMovementORM.id)
        if product_id:
            query = query.where(StockMovementORM.product_id == product_id)
        return list((await session.execute(query)).scalars())


async def product_stock(product_id: str) -> tuple[int, int]:
    async with AsyncSessionLocal() as session:
        product = await session.get(ProductORM, product_id)
        return product.stock, product.reserved_stock


@pytest.fixture()
def staff_ids(monkeypatch):
    monkeypatch.setenv("STOCK_SESSION_COOKIE_SECURE", "false")
    login_throttle.clear()
    yield run(reset_stock_database())
    login_throttle.clear()


@pytest.fixture()
def client(staff_ids):
    with TestClient(app) as test_client:
        yield test_client
    asyncio.run(engine.dispose())


def login(client: TestClient, username: str = "admin", password: str = PASSWORD):
    return client.post(
        "/api/estoque/login",
        json={"username": username, "password": password},
        headers=PANEL,
    )


def post_movement(client: TestClient, **payload):
    return client.post("/api/estoque/movimentos", json=payload, headers=PANEL)


# --- Senhas e login ---------------------------------------------------------


def test_password_hash_roundtrip_and_rejects_short_passwords():
    stored = hash_password("uma-senha-boa")
    assert stored.startswith("scrypt$")
    assert "uma-senha-boa" not in stored
    assert verify_password("uma-senha-boa", stored)
    assert not verify_password("outra-senha", stored)
    assert not verify_password("uma-senha-boa", "formato-invalido")
    with pytest.raises(ValueError):
        hash_password("curta")


def test_panel_page_and_api_require_login(client):
    page = client.get("/estoque")
    assert page.status_code == 200
    assert "Controle de" in page.text
    for path in ("/api/estoque/me", "/api/estoque/produtos", "/api/estoque/movimentos", "/api/estoque/resumo"):
        assert client.get(path).status_code == 401, path
    assert post_movement(
        client, product_id="original", movement_type="RESTOCK", quantity=1
    ).status_code == 401


def test_login_requires_panel_header(client):
    response = client.post(
        "/api/estoque/login",
        json={"username": "admin", "password": PASSWORD},
    )
    assert response.status_code == 403


def test_login_sets_hardened_cookie_and_logout_revokes_session(client):
    assert login(client, password="errada-123").status_code == 401
    response = login(client)
    assert response.status_code == 200
    assert response.json()["role"] == "ADMIN"
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie
    assert "samesite=strict" in cookie
    token = client.cookies.get(SESSION_COOKIE_NAME)
    assert token

    async def stored_hashes():
        async with AsyncSessionLocal() as session:
            return list((await session.execute(select(StaffSessionORM.token_hash))).scalars())

    hashes = run(stored_hashes())
    assert hashes and token not in hashes

    assert client.get("/api/estoque/me").json()["username"] == "admin"
    assert client.post("/api/estoque/logout", headers=PANEL).status_code == 204
    client.cookies.set(SESSION_COOKIE_NAME, token)
    assert client.get("/api/estoque/me").status_code == 401


def test_repeated_failures_lock_the_account(client, monkeypatch):
    monkeypatch.setenv("STOCK_LOGIN_MAX_ATTEMPTS", "3")
    for _ in range(3):
        assert login(client, "caixa", "errada-123").status_code == 401
    locked = login(client, "caixa", PASSWORD)
    assert locked.status_code == 429


def test_deactivated_user_loses_access(client, staff_ids):
    assert login(client, "caixa").status_code == 200

    async def deactivate():
        async with AsyncSessionLocal() as session, session.begin():
            await session.execute(
                update(StaffUserORM)
                .where(StaffUserORM.id == staff_ids["caixa"])
                .values(active=False)
            )

    run(deactivate())
    assert client.get("/api/estoque/me").status_code == 401
    assert login(client, "caixa").status_code == 401


# --- Movimentações ---------------------------------------------------------


def test_lists_all_flavors_with_physical_and_available_stock(client):
    login(client)
    products = client.get("/api/estoque/produtos").json()
    assert {product["id"] for product in products} == set(INITIAL_STOCK)
    tropical = next(product for product in products if product["id"] == "tropical")
    assert tropical["stock"] == 18
    assert tropical["available"] == 18
    assert tropical["out_today"] == 0


def test_restock_increments_stock_and_records_operator(client, staff_ids):
    login(client, "caixa")
    response = post_movement(
        client,
        product_id="original",
        movement_type="RESTOCK",
        quantity=6,
        note="Lote da semana",
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["product"]["stock"] == 30
    assert body["movement"]["delta"] == 6
    assert body["movement"]["staff_username"] == "caixa"

    movements = run(fetch_movements("original"))
    assert len(movements) == 1
    movement = movements[0]
    assert movement.movement_type == "RESTOCK"
    assert movement.source == "PAINEL"
    assert movement.staff_user_id == staff_ids["caixa"]
    assert (movement.stock_before, movement.stock_after) == (24, 30)
    assert movement.note == "Lote da semana"
    assert run(product_stock("original")) == (30, 0)


def test_manual_sale_requires_buyer_and_records_who_bought(client):
    login(client, "caixa")
    missing = post_movement(
        client, product_id="tropical", movement_type="MANUAL_SALE", quantity=2
    )
    assert missing.status_code == 422
    assert run(fetch_movements()) == []

    response = post_movement(
        client,
        product_id="tropical",
        movement_type="MANUAL_SALE",
        quantity=2,
        buyer_name="  Ana Souza ",
        buyer_email="Ana@Citi.org.br",
    )
    assert response.status_code == 201, response.text
    movement = run(fetch_movements("tropical"))[0]
    assert movement.delta == -2
    assert movement.buyer_name == "Ana Souza"
    assert movement.buyer_email == "ana@citi.org.br"
    assert movement.created_at is not None
    assert run(product_stock("tropical")) == (16, 0)

    summary = client.get("/api/estoque/resumo").json()
    assert summary["units_out_today"] == 2
    assert summary["manual_sales_today"] == 2
    history = client.get("/api/estoque/movimentos", params={"direction": "out"}).json()
    assert history[0]["buyer_name"] == "Ana Souza"


def test_cannot_remove_more_than_physical_stock(client):
    login(client)
    response = post_movement(
        client, product_id="melancia", movement_type="LOSS", quantity=17
    )
    assert response.status_code == 409
    assert run(product_stock("melancia")) == (16, 0)
    assert run(fetch_movements()) == []


def test_cannot_remove_units_reserved_for_pending_pix(client):
    order = client.post(
        "/api/orders",
        json={
            "buyer_name": "Comprador PIX",
            "payer_email": "pix@example.com",
            "pricing_audience": "MEMBER",
            "items": [{"product_id": "melancia", "quantity": 10}],
        },
    )
    assert order.status_code == 201
    login(client)
    blocked = post_movement(
        client,
        product_id="melancia",
        movement_type="MANUAL_SALE",
        quantity=7,
        buyer_name="Outra pessoa",
    )
    assert blocked.status_code == 409
    assert "reservadas" in blocked.json()["detail"]
    allowed = post_movement(
        client,
        product_id="melancia",
        movement_type="MANUAL_SALE",
        quantity=6,
        buyer_name="Outra pessoa",
    )
    assert allowed.status_code == 201
    assert allowed.json()["product"]["available"] == 0
    assert run(product_stock("melancia")) == (10, 10)


def test_inventory_adjustment_is_admin_only_and_requires_reason(client):
    login(client, "caixa")
    forbidden = post_movement(
        client,
        product_id="original",
        movement_type="ADJUSTMENT",
        counted_stock=20,
        note="Contagem",
    )
    assert forbidden.status_code == 403

    login(client, "admin")
    no_reason = post_movement(
        client, product_id="original", movement_type="ADJUSTMENT", counted_stock=20
    )
    assert no_reason.status_code == 422
    same = post_movement(
        client,
        product_id="original",
        movement_type="ADJUSTMENT",
        counted_stock=24,
        note="Contagem",
    )
    assert same.status_code == 409
    response = post_movement(
        client,
        product_id="original",
        movement_type="ADJUSTMENT",
        counted_stock=21,
        note="Contagem semanal",
    )
    assert response.status_code == 201
    assert response.json()["movement"]["delta"] == -3
    assert run(product_stock("original")) == (21, 0)
    # Ajuste é correção de inventário, não saída de latas.
    summary = client.get("/api/estoque/resumo").json()
    assert summary["units_out_today"] == 0
    assert response.json()["product"]["out_today"] == 0


def test_marketplace_types_cannot_be_posted_from_panel(client):
    login(client)
    response = post_movement(
        client, product_id="original", movement_type="MARKETPLACE_SALE", quantity=1
    )
    assert response.status_code == 422


# --- Integração com o marketplace ----------------------------------------------


def test_paid_marketplace_order_is_recorded_with_buyer_and_time(client):
    created = client.post(
        "/api/orders",
        json={
            "buyer_name": "Carlos Andrade",
            "payer_email": "carlos@example.com",
            "pricing_audience": "MEMBER",
            "items": [
                {"product_id": "tropical", "quantity": 3},
                {"product_id": "original", "quantity": 1},
            ],
        },
    ).json()
    assert run(fetch_movements()) == []  # reservar não é saída de estoque

    paid = client.post(f"/api/mock/pay/{created['payment']['txid']}")
    assert paid.status_code == 200
    assert paid.json()["status"] == "PAID"

    movements = run(fetch_movements())
    assert sorted((m.product_id, m.delta) for m in movements) == [
        ("original", -1),
        ("tropical", -3),
    ]
    for movement in movements:
        assert movement.movement_type == "MARKETPLACE_SALE"
        assert movement.source == "MARKETPLACE"
        assert str(movement.order_id) == created["id"]
        assert movement.buyer_name == "Carlos Andrade"
        assert movement.buyer_email == "carlos@example.com"
        assert movement.unit_price == Decimal("7.50")
        assert movement.staff_user_id is None

    # Repetir a confirmação não gera novas linhas.
    client.post(f"/api/mock/pay/{created['payment']['txid']}")
    assert len(run(fetch_movements())) == 2


def test_direct_sql_changes_are_still_audited_as_untracked(staff_ids):
    async def direct_update():
        async with AsyncSessionLocal() as session, session.begin():
            await session.execute(
                text("UPDATE products SET stock = stock - 4 WHERE id = 'original'")
            )

    run(direct_update())
    movement = run(fetch_movements("original"))[0]
    assert movement.movement_type == "UNTRACKED"
    assert movement.source == "BANCO"
    assert movement.delta == -4
    assert movement.db_user


def test_invalid_context_never_blocks_a_stock_change(staff_ids):
    async def bad_context_update():
        async with AsyncSessionLocal() as session, session.begin():
            await session.execute(
                text("SELECT set_config('redbull.stock_context', '{quebrado', true)")
            )
            await session.execute(
                text("UPDATE products SET stock = stock - 1 WHERE id = 'tropical'")
            )
            await session.execute(
                text(
                    "SELECT set_config('redbull.stock_context', "
                    "'{\"movement_type\": \"RESTOCK\"}', true)"
                )
            )
            await session.execute(
                text("UPDATE products SET stock = stock - 1 WHERE id = 'tropical'")
            )

    run(bad_context_update())
    movements = run(fetch_movements("tropical"))
    assert [m.movement_type for m in movements] == ["UNTRACKED", "UNTRACKED"]
    assert "inválido" in movements[0].note
    assert "incompatível" in movements[1].note
    assert run(product_stock("tropical")) == (16, 0)


def test_concurrent_sales_of_the_last_can_do_not_oversell(staff_ids):
    async def scenario():
        async with AsyncSessionLocal() as session, session.begin():
            await session.execute(
                update(ProductORM).where(ProductORM.id == "melancia").values(stock=1)
            )
        staff = StaffUser(
            id=staff_ids["caixa"],
            username="caixa",
            full_name="Usuário caixa",
            role=StaffRole.OPERADOR,
        )

        async def sell(buyer: str):
            async with AsyncSessionLocal() as session:
                try:
                    await apply_panel_movement(
                        session,
                        StockMovementCreate(
                            product_id="melancia",
                            movement_type=StockMovementType.MANUAL_SALE,
                            quantity=1,
                            buyer_name=buyer,
                        ),
                        staff,
                    )
                    return "ok"
                except Exception as exc:  # HTTPException 409
                    return getattr(exc, "status_code", exc)

        return await asyncio.gather(sell("Primeiro"), sell("Segundo"))

    results = run(scenario())
    assert sorted(results, key=str) == [409, "ok"]
    assert run(product_stock("melancia")) == (0, 0)
    sales = [m for m in run(fetch_movements("melancia")) if m.movement_type == "MANUAL_SALE"]
    assert len(sales) == 1


# --- Endurecimento -----------------------------------------------------------


def test_panel_page_is_served_with_security_headers_only_from_its_route(client):
    page = client.get("/estoque")
    assert page.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
    assert client.get("/static/estoque/index.html").status_code == 404
    assert client.get("/static/estoque/estoque.js").status_code == 200


def test_non_ascii_session_cookie_is_rejected_not_crashing(client):
    raw_cookie = f"{SESSION_COOKIE_NAME}=sessão-inválida".encode("utf-8")
    response = client.get("/api/estoque/me", headers={b"cookie": raw_cookie})
    assert response.status_code == 401


def test_ip_is_throttled_after_repeated_failures(client, monkeypatch):
    monkeypatch.setenv("STOCK_LOGIN_IP_MAX_FAILURES", "2")
    assert login(client, "nao-existe", "x").status_code == 401
    assert login(client, "outro-inexistente", "x").status_code == 401
    assert login(client, "admin").status_code == 429


@pytest.mark.parametrize(
    "field,value",
    [("buyer_name", "Ana\x00Souza"), ("buyer_name", "Ana\nSouza"), ("note", "nota\x00")],
)
def test_control_characters_are_rejected_before_reaching_the_ledger(client, field, value):
    login(client, "caixa")
    payload = {
        "product_id": "tropical",
        "movement_type": "MANUAL_SALE",
        "quantity": 1,
        "buyer_name": "Ana Souza",
        field: value,
    }
    response = post_movement(client, **payload)
    assert response.status_code == 422
    assert run(fetch_movements()) == []
    assert run(product_stock("tropical")) == (18, 0)


def test_multiline_note_is_kept(client):
    login(client, "caixa")
    response = post_movement(
        client,
        product_id="tropical",
        movement_type="LOSS",
        quantity=1,
        note="Lata amassada\nna entrega",
    )
    assert response.status_code == 201
    assert run(fetch_movements("tropical"))[0].note == "Lata amassada\nna entrega"


def test_ledger_trigger_works_for_a_role_without_grants_on_the_ledger(staff_ids):
    """O trigger é SECURITY DEFINER: uma role da aplicação só com acesso a
    products consegue vender, e a linha é gravada mesmo assim."""

    async def scenario():
        async with AsyncSessionLocal() as session, session.begin():
            await session.execute(
                text(
                    "DO $$ BEGIN "
                    "IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'stock_ledger_probe') "
                    "THEN CREATE ROLE stock_ledger_probe NOLOGIN; END IF; END $$"
                )
            )
            await session.execute(text("GRANT USAGE ON SCHEMA public TO stock_ledger_probe"))
            await session.execute(
                text("GRANT SELECT, UPDATE ON products TO stock_ledger_probe")
            )
        async with AsyncSessionLocal() as session, session.begin():
            await session.execute(text("SET LOCAL ROLE stock_ledger_probe"))
            await session.execute(
                text("UPDATE products SET stock = stock - 2 WHERE id = 'melancia'")
            )

    run(scenario())
    movement = run(fetch_movements("melancia"))[0]
    assert movement.delta == -2
    assert movement.movement_type == "UNTRACKED"
    assert run(product_stock("melancia")) == (14, 0)


def test_panel_rolls_back_when_the_ledger_trigger_is_missing(client):
    login(client, "caixa")

    async def toggle(enabled: bool):
        async with AsyncSessionLocal() as session, session.begin():
            action = "ENABLE" if enabled else "DISABLE"
            await session.execute(
                text(f"ALTER TABLE products {action} TRIGGER trg_products_stock_ledger")
            )

    run(toggle(False))
    try:
        response = post_movement(
            client, product_id="original", movement_type="RESTOCK", quantity=3
        )
    finally:
        run(toggle(True))
    assert response.status_code == 500
    assert run(product_stock("original")) == (24, 0)
    assert run(fetch_movements()) == []


# --- Preços ------------------------------------------------------------------


async def snapshot_price_tiers() -> dict[int, bool]:
    async with AsyncSessionLocal() as session:
        rows = await session.execute(select(PriceTierORM.id, PriceTierORM.active))
        return dict(rows.all())


async def restore_price_tiers(original: dict[int, bool]) -> None:
    async with AsyncSessionLocal() as session, session.begin():
        await session.execute(delete(OrderORM))
        await session.execute(delete(PriceTierORM).where(PriceTierORM.id.not_in(original)))
        for tier_id, active in original.items():
            await session.execute(
                update(PriceTierORM).where(PriceTierORM.id == tier_id).values(active=active)
            )


@pytest.fixture()
def restore_prices(staff_ids):
    original = run(snapshot_price_tiers())
    yield
    run(restore_price_tiers(original))


def put_price(client: TestClient, audience: str, base_price, headers=PANEL):
    return client.put(
        "/api/estoque/precos",
        json={"pricing_audience": audience, "base_price": base_price},
        headers=headers,
    )


def public_tiers(client: TestClient, audience: str) -> list[tuple[int, int | None, float]]:
    response = client.get("/api/price-tiers", params={"pricing_audience": audience})
    assert response.status_code == 200
    return [
        (tier["min_quantity"], tier["max_quantity"], tier["unit_price"])
        for tier in response.json()
    ]


def test_admin_sets_base_price_and_tiers_follow_the_rule(client, restore_prices):
    login(client, "admin")
    before = run(snapshot_price_tiers())

    response = put_price(client, "MEMBER", "9.00")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["base_price"] == 9.0
    assert [(t["min_quantity"], t["max_quantity"], t["unit_price"]) for t in body["tiers"]] == [
        (1, 1, 9.0),
        (2, 3, 8.5),
        (4, None, 8.0),
    ]
    assert public_tiers(client, "MEMBER") == [(1, 1, 9.0), (2, 3, 8.5), (4, None, 8.0)]

    # Faixas antigas são desativadas, não apagadas (pedidos antigos as referenciam).
    after = run(snapshot_price_tiers())
    assert set(before) <= set(after)
    new_ids = {tier["id"] for tier in body["tiers"]}
    assert new_ids.isdisjoint(before)
    assert sum(after.values()) == sum(before.values())

    panel = client.get("/api/estoque/precos")
    assert panel.status_code == 200
    tables = {table["pricing_audience"]: table for table in panel.json()}
    assert tables["MEMBER"]["base_price"] == 9.0

    # O marketplace passa a cobrar pela nova faixa: 2 latas = 2 × 8,50.
    order = client.post(
        "/api/orders",
        json={
            "buyer_name": "Comprador",
            "payer_email": "comprador@example.com",
            "pricing_audience": "MEMBER",
            "items": [{"product_id": "original", "quantity": 2}],
        },
    )
    assert order.status_code == 201, order.text
    assert order.json()["total"] == 17.0


def test_non_member_has_a_single_price_without_promotion(client, restore_prices):
    login(client, "admin")
    assert put_price(client, "NON_MEMBER", "13").status_code == 200
    assert public_tiers(client, "NON_MEMBER") == [(1, None, 13.0)]
    # O outro público não muda.
    assert public_tiers(client, "MEMBER") == [(1, 1, 8.5), (2, 3, 8.0), (4, None, 7.5)]


def test_only_admins_change_prices(client, restore_prices):
    assert put_price(client, "MEMBER", "9.00").status_code == 401
    login(client, "caixa")
    assert client.get("/api/estoque/precos").status_code == 200
    assert put_price(client, "MEMBER", "9.00").status_code == 403
    login(client, "admin")
    assert put_price(client, "MEMBER", "9.00", headers={}).status_code == 403
    assert public_tiers(client, "MEMBER") == [(1, 1, 8.5), (2, 3, 8.0), (4, None, 7.5)]


@pytest.mark.parametrize("base_price", ["1.00", "0.50", "0", "-5", "8.555", "1000.01", "abc"])
def test_invalid_base_price_is_rejected(client, restore_prices, base_price):
    login(client, "admin")
    assert put_price(client, "MEMBER", base_price).status_code == 422


def test_same_price_is_a_conflict_and_changes_nothing(client, restore_prices):
    login(client, "admin")
    before = run(snapshot_price_tiers())
    assert put_price(client, "MEMBER", "8.50").status_code == 409
    assert run(snapshot_price_tiers()) == before


def test_concurrent_price_changes_leave_a_single_active_table(staff_ids, restore_prices):
    admin = StaffUser(
        id=staff_ids["admin"],
        username="admin",
        full_name="Usuário admin",
        role=StaffRole.ADMIN,
    )

    async def scenario():
        async def change(base: str):
            async with AsyncSessionLocal() as session:
                await update_price_table(
                    session,
                    PriceTableUpdate(pricing_audience="MEMBER", base_price=base),
                    admin,
                )

        await asyncio.gather(change("9.00"), change("10.00"))
        async with AsyncSessionLocal() as session:
            rows = await session.execute(
                select(PriceTierORM.min_quantity, PriceTierORM.unit_price).where(
                    PriceTierORM.pricing_audience == "MEMBER",
                    PriceTierORM.active.is_(True),
                )
            )
            return sorted(rows.all())

    active = run(scenario())
    assert len(active) == 3
    assert active[0][1] in (Decimal("9.00"), Decimal("10.00"))
