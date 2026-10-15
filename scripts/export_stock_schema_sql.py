"""Regera sql/estoque_schema.sql a partir da migration c3f8a1d27b54.

    python -m scripts.export_stock_schema_sql

O arquivo SQL é a mesma lista de comandos da migration, para revisão e
aplicação manual. tests/test_stock_schema_sql.py falha se os dois divergirem.
"""

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "app/db/migrations/versions/c3f8a1d27b54_add_stock_control_ledger.py"
OUTPUT = ROOT / "sql/estoque_schema.sql"

HEADER = """-- =====================================================================
-- Controle de estoque Red Bull CITi — modelo de dados (PostgreSQL 11+)
--
-- Mesmo conteúdo da revisão Alembic c3f8a1d27b54 (parte de 7e03d1bf5a42).
-- Caminho recomendado:  alembic upgrade head
-- Este arquivo existe para revisão, diagramas (DBeaver/pgAdmin) e para
-- quem precisar aplicar o modelo manualmente. É idempotente: pode ser
-- executado de novo sem erro, e o `alembic upgrade head` posterior
-- também funciona (usa IF NOT EXISTS / CREATE OR REPLACE).
--
-- Pré-requisito: tabelas products, orders e order_items do marketplace
-- (ou seja, o banco precisa estar na revisão 7e03d1bf5a42).
--
-- NÃO altera: users, stock_adjustments, products.description/color/price.
--
-- Tabelas criadas:
--   staff_users      operadores do painel /estoque (login)
--   staff_sessions   sessões de login (só o hash do token)
--   stock_movements  livro-razão: quem comprou, quantas latas saíram e quando
-- Trigger:
--   trg_products_stock_ledger  registra TODA alteração de products.stock
-- Views:
--   vw_estoque_atual, vw_movimentacoes_estoque, vw_saidas_estoque
--
-- Gerado a partir de UPGRADE_STATEMENTS da migration; não edite à mão sem
-- alterar a migration (tests/test_stock_schema_sql.py confere os dois).
-- =====================================================================

"""


def main() -> None:
    spec = importlib.util.spec_from_file_location("stock_ledger_migration", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    body = "\n".join(statement.strip() + ";\n" for statement in module.UPGRADE_STATEMENTS)
    OUTPUT.write_text(HEADER + "BEGIN;\n\n" + body + "\nCOMMIT;\n", encoding="utf-8")
    print(f"{OUTPUT.relative_to(ROOT)} atualizado ({len(module.UPGRADE_STATEMENTS)} comandos).")


if __name__ == "__main__":
    main()
