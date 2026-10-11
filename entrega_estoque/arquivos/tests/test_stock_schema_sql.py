import importlib.util
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "app/db/migrations/versions/c3f8a1d27b54_add_stock_control_ledger.py"
SCHEMA_SQL = ROOT / "sql/estoque_schema.sql"


def _normalize(sql: str) -> str:
    return re.sub(r"\s+", " ", sql).strip()


def _load_migration():
    spec = importlib.util.spec_from_file_location("stock_ledger_migration", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_standalone_sql_matches_alembic_migration_in_order():
    migration = _load_migration()
    script = _normalize(SCHEMA_SQL.read_text(encoding="utf-8"))
    position = 0
    for statement in migration.UPGRADE_STATEMENTS:
        found = script.find(_normalize(statement), position)
        assert found >= 0, f"Comando ausente ou fora de ordem em sql/estoque_schema.sql:\n{statement[:120]}"
        position = found


def test_migration_preserves_external_tables():
    migration = _load_migration()
    every_statement = " ".join(
        migration.UPGRADE_STATEMENTS + migration.DOWNGRADE_STATEMENTS
    ).lower()
    assert "stock_adjustments" not in every_statement
    assert not re.search(r"\b(drop|alter|truncate)\s+table\s+(if exists\s+)?users\b", every_statement)
    assert "alter table products" not in every_statement
