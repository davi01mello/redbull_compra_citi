import importlib

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import inspect

from app.db.base import engine


def test_alembic_has_one_explicit_merge_head():
    scripts = ScriptDirectory.from_config(Config("alembic.ini"))
    assert scripts.get_heads() == ["c3f8a1d27b54"]
    assert scripts.get_revision("c3f8a1d27b54").down_revision == "7e03d1bf5a42"
    revision = scripts.get_revision("4c7b91e2a6d0")
    assert revision.down_revision == "e91a7c4d2f30"


def test_upgraded_test_database_matches_nullable_email_model():
    async def columns():
        async with engine.connect() as connection:
            result = await connection.run_sync(
                lambda sync_connection: {
                    column["name"]: column
                    for column in inspect(sync_connection).get_columns("orders")
                }
            )
        await engine.dispose()
        return result

    import asyncio

    order_columns = asyncio.run(columns())
    assert order_columns["payer_email"]["nullable"] is True
    assert order_columns["payer_email"]["type"].length == 254


def test_payer_email_downgrade_only_removes_owned_column(monkeypatch):
    migration = importlib.import_module(
        "app.db.migrations.versions.4c7b91e2a6d0_add_payer_email_to_orders"
    )
    calls = []
    monkeypatch.setattr(
        migration.op,
        "drop_column",
        lambda table, column: calls.append((table, column)),
    )
    migration.downgrade()
    assert calls == [("orders", "payer_email")]
