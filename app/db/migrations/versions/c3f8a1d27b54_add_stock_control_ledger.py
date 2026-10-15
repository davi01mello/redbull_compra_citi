"""add stock control ledger, staff users and sessions

Recria no banco o controle de estoque do marketplace:

- ``staff_users``: operadores do painel de estoque (login);
- ``staff_sessions``: sessões de login do painel (token guardado só como hash);
- ``stock_movements``: livro-razão imutável de toda alteração em
  ``products.stock`` — quem comprou, quantas unidades saíram e o horário;
- ``fn_stock_movements_ledger`` + ``trg_products_stock_ledger``: o próprio
  banco registra a movimentação sempre que ``products.stock`` muda, inclusive
  em vendas pagas pelo marketplace e em alterações feitas direto via SQL;
- views ``vw_estoque_atual``, ``vw_movimentacoes_estoque`` e
  ``vw_saidas_estoque`` para consulta.

Não altera ``users``, ``stock_adjustments`` nem as colunas legadas
``products.description``/``color``/``price``. Todos os comandos são
idempotentes (``IF NOT EXISTS`` / ``CREATE OR REPLACE``), então a revisão
também funciona em bancos onde ``sql/estoque_schema.sql`` já foi aplicado.

O downgrade apaga o histórico de movimentações e os operadores. Faça backup
antes de executá-lo em qualquer banco real.

Revision ID: c3f8a1d27b54
Revises: 7e03d1bf5a42
Create Date: 2026-10-09
"""

from typing import Sequence, Union

from alembic import op


revision: str = "c3f8a1d27b54"
down_revision: Union[str, Sequence[str], None] = "7e03d1bf5a42"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Cada item é um comando SQL completo. A mesma lista gera
# sql/estoque_schema.sql (ver tests/test_stock_schema_sql.py).
UPGRADE_STATEMENTS: tuple[str, ...] = (
    # 1) Operadores do painel de estoque ------------------------------------
    """
CREATE TABLE IF NOT EXISTS staff_users (
    id                     BIGSERIAL    PRIMARY KEY,
    username               VARCHAR(50)  NOT NULL,
    full_name              VARCHAR(120) NOT NULL,
    password_hash          VARCHAR(255) NOT NULL,
    role                   VARCHAR(20)  NOT NULL DEFAULT 'OPERADOR',
    active                 BOOLEAN      NOT NULL DEFAULT true,
    failed_login_attempts  INTEGER      NOT NULL DEFAULT 0,
    locked_until           TIMESTAMPTZ,
    last_login_at          TIMESTAMPTZ,
    created_at             TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at             TIMESTAMPTZ  NOT NULL DEFAULT now(),
    CONSTRAINT uq_staff_users_username UNIQUE (username),
    CONSTRAINT ck_staff_users_role CHECK (role IN ('ADMIN', 'OPERADOR')),
    CONSTRAINT ck_staff_users_username_format
        CHECK (username ~ '^[a-z0-9._-]{3,50}$'),
    CONSTRAINT ck_staff_users_failed_login_attempts
        CHECK (failed_login_attempts >= 0)
)
""",
    """
COMMENT ON TABLE staff_users IS
    'Operadores do painel de estoque. Senha guardada apenas como hash scrypt.'
""",
    # 2) Sessões de login ----------------------------------------------------
    """
CREATE TABLE IF NOT EXISTS staff_sessions (
    id             BIGSERIAL    PRIMARY KEY,
    staff_user_id  BIGINT       NOT NULL,
    token_hash     VARCHAR(64)  NOT NULL,
    created_at     TIMESTAMPTZ  NOT NULL DEFAULT now(),
    expires_at     TIMESTAMPTZ  NOT NULL,
    revoked_at     TIMESTAMPTZ,
    user_agent     VARCHAR(255),
    ip_address     VARCHAR(45),
    CONSTRAINT uq_staff_sessions_token_hash UNIQUE (token_hash),
    CONSTRAINT fk_staff_sessions_staff_user_id
        FOREIGN KEY (staff_user_id) REFERENCES staff_users (id) ON DELETE CASCADE
)
""",
    """
CREATE INDEX IF NOT EXISTS ix_staff_sessions_staff_user_id
    ON staff_sessions (staff_user_id)
""",
    """
COMMENT ON TABLE staff_sessions IS
    'Sessões do painel de estoque. O token do cookie nunca é salvo, só o SHA-256.'
""",
    # 3) Livro-razão de estoque ----------------------------------------------
    """
CREATE TABLE IF NOT EXISTS stock_movements (
    id                    BIGSERIAL      PRIMARY KEY,
    product_id            VARCHAR(50)    NOT NULL,
    product_name          VARCHAR(120)   NOT NULL,
    movement_type         VARCHAR(30)    NOT NULL,
    source                VARCHAR(20)    NOT NULL,
    quantity              INTEGER        NOT NULL,
    delta                 INTEGER        NOT NULL,
    stock_before          INTEGER        NOT NULL,
    stock_after           INTEGER        NOT NULL,
    reserved_stock_after  INTEGER        NOT NULL,
    order_id              UUID,
    staff_user_id         BIGINT,
    buyer_name            VARCHAR(80),
    buyer_email           VARCHAR(254),
    unit_price            NUMERIC(10, 2),
    note                  VARCHAR(500),
    db_user               VARCHAR(63)    NOT NULL DEFAULT session_user,
    created_at            TIMESTAMPTZ    NOT NULL DEFAULT now(),
    CONSTRAINT fk_stock_movements_product_id
        FOREIGN KEY (product_id) REFERENCES products (id),
    CONSTRAINT fk_stock_movements_order_id
        FOREIGN KEY (order_id) REFERENCES orders (id) ON DELETE SET NULL,
    CONSTRAINT fk_stock_movements_staff_user_id
        FOREIGN KEY (staff_user_id) REFERENCES staff_users (id),
    CONSTRAINT ck_stock_movements_type CHECK (
        movement_type IN (
            'INITIAL_STOCK', 'RESTOCK', 'MANUAL_SALE', 'LOSS',
            'ADJUSTMENT', 'MARKETPLACE_SALE', 'UNTRACKED'
        )
    ),
    CONSTRAINT ck_stock_movements_source
        CHECK (source IN ('PAINEL', 'MARKETPLACE', 'BANCO')),
    CONSTRAINT ck_stock_movements_quantity
        CHECK (quantity > 0 AND quantity = abs(delta)),
    CONSTRAINT ck_stock_movements_balance
        CHECK (stock_after = stock_before + delta),
    CONSTRAINT ck_stock_movements_direction CHECK (
        (movement_type IN ('INITIAL_STOCK', 'RESTOCK') AND delta > 0)
        OR (movement_type IN ('MANUAL_SALE', 'LOSS', 'MARKETPLACE_SALE') AND delta < 0)
        OR movement_type IN ('ADJUSTMENT', 'UNTRACKED')
    ),
    CONSTRAINT ck_stock_movements_manual_sale_buyer
        CHECK (movement_type <> 'MANUAL_SALE' OR buyer_name IS NOT NULL)
)
""",
    """
CREATE INDEX IF NOT EXISTS ix_stock_movements_product_id_created_at
    ON stock_movements (product_id, created_at)
""",
    """
CREATE INDEX IF NOT EXISTS ix_stock_movements_created_at
    ON stock_movements (created_at)
""",
    """
CREATE INDEX IF NOT EXISTS ix_stock_movements_order_id
    ON stock_movements (order_id)
""",
    """
CREATE INDEX IF NOT EXISTS ix_stock_movements_staff_user_id
    ON stock_movements (staff_user_id)
""",
    """
COMMENT ON TABLE stock_movements IS
    'Livro-razão de estoque: uma linha por alteração de products.stock. Preenchido pelo trigger trg_products_stock_ledger.'
""",
    # 4) Trigger: o banco registra toda alteração de products.stock ----------
    #
    # O contexto (tipo, operador, comprador, pedido) vem da configuração
    # transacional "redbull.stock_context", definida pela aplicação com
    # set_config(..., true) na mesma transação que altera o estoque. Sem
    # contexto, a linha é registrada como UNTRACKED com origem BANCO.
    # O trigger nunca bloqueia uma venda: contexto inválido vira UNTRACKED.
    """
CREATE OR REPLACE FUNCTION fn_stock_movements_ledger()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
AS $fn$
DECLARE
    v_raw          text;
    v_ctx          jsonb := '{}'::jsonb;
    v_before       integer;
    v_delta        integer;
    v_type         text;
    v_source       text;
    v_order_id     uuid;
    v_staff_id     bigint;
    v_buyer_name   text;
    v_buyer_email  text;
    v_unit_price   numeric(10, 2);
    v_note         text;
BEGIN
    IF TG_OP = 'INSERT' THEN
        v_before := 0;
    ELSE
        v_before := OLD.stock;
    END IF;
    v_delta := NEW.stock - v_before;
    IF v_delta = 0 THEN
        RETURN NEW;
    END IF;

    -- Cada parte do contexto é lida isoladamente: um campo ruim não apaga
    -- os demais (principalmente o operador) e nunca aborta a transação.
    BEGIN
        v_raw := current_setting('redbull.stock_context', true);
        IF v_raw IS NOT NULL AND btrim(v_raw) <> '' THEN
            v_ctx := v_raw::jsonb;
        END IF;
    EXCEPTION WHEN others THEN
        v_ctx := '{}'::jsonb;
        v_note := '[contexto de estoque inválido ignorado]';
    END;
    IF jsonb_typeof(v_ctx) IS DISTINCT FROM 'object' THEN
        v_ctx := '{}'::jsonb;
    END IF;

    v_type := v_ctx ->> 'movement_type';
    v_source := v_ctx ->> 'source';
    v_buyer_name := left(NULLIF(btrim(v_ctx ->> 'buyer_name'), ''), 80);
    v_buyer_email := left(NULLIF(btrim(v_ctx ->> 'buyer_email'), ''), 254);
    v_note := concat_ws(' ', v_note, NULLIF(btrim(v_ctx ->> 'note'), ''));
    BEGIN
        v_order_id := NULLIF(v_ctx ->> 'order_id', '')::uuid;
    EXCEPTION WHEN others THEN
        v_order_id := NULL;
    END;
    BEGIN
        v_staff_id := NULLIF(v_ctx ->> 'staff_user_id', '')::bigint;
    EXCEPTION WHEN others THEN
        v_staff_id := NULL;
    END;
    BEGIN
        v_unit_price := NULLIF(v_ctx ->> 'unit_price', '')::numeric(10, 2);
    EXCEPTION WHEN others THEN
        v_unit_price := NULL;
    END;

    IF v_type IS NULL THEN
        v_type := CASE WHEN TG_OP = 'INSERT' THEN 'INITIAL_STOCK' ELSE 'UNTRACKED' END;
    END IF;
    IF v_source IS NULL OR v_source NOT IN ('PAINEL', 'MARKETPLACE', 'BANCO') THEN
        v_source := 'BANCO';
    END IF;

    IF NOT (
        (v_type IN ('INITIAL_STOCK', 'RESTOCK') AND v_delta > 0)
        OR (v_type IN ('MANUAL_SALE', 'LOSS', 'MARKETPLACE_SALE') AND v_delta < 0)
        OR v_type IN ('ADJUSTMENT', 'UNTRACKED')
    ) OR (v_type = 'MANUAL_SALE' AND v_buyer_name IS NULL) THEN
        v_note := concat_ws(' ', '[tipo ' || left(v_type, 40) || ' incompatível com a alteração]', v_note);
        v_type := 'UNTRACKED';
    END IF;

    IF v_order_id IS NOT NULL THEN
        SELECT COALESCE(v_buyer_name, left(o.buyer_name, 80)),
               COALESCE(v_buyer_email, left(o.payer_email, 254))
          INTO v_buyer_name, v_buyer_email
          FROM orders AS o
         WHERE o.id = v_order_id;
        IF NOT FOUND THEN
            v_order_id := NULL;
        ELSE
            SELECT COALESCE(v_unit_price, max(item.unit_price))
              INTO v_unit_price
              FROM order_items AS item
             WHERE item.order_id = v_order_id
               AND item.product_id = NEW.id;
        END IF;
    END IF;

    IF v_staff_id IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM staff_users WHERE id = v_staff_id) THEN
        v_staff_id := NULL;
    END IF;

    INSERT INTO stock_movements (
        product_id, product_name, movement_type, source,
        quantity, delta, stock_before, stock_after, reserved_stock_after,
        order_id, staff_user_id, buyer_name, buyer_email, unit_price, note, db_user
    ) VALUES (
        NEW.id, left(NEW.name, 120), v_type, v_source,
        abs(v_delta), v_delta, v_before, NEW.stock, NEW.reserved_stock,
        v_order_id, v_staff_id, v_buyer_name, v_buyer_email, v_unit_price,
        left(NULLIF(v_note, ''), 500), left(session_user, 63)
    );
    RETURN NEW;
END;
$fn$
""",
    # SECURITY DEFINER: o trigger grava com os privilégios do dono da função,
    # então uma role da aplicação sem GRANT em stock_movements não aborta a
    # venda. O search_path fica fixo no schema atual para não resolver nomes
    # em schemas/tabelas temporárias de quem dispara o trigger.
    """
DO $sp$
BEGIN
    EXECUTE format(
        'ALTER FUNCTION fn_stock_movements_ledger() SET search_path = %I, pg_temp',
        current_schema()
    );
END
$sp$
""",
    """
REVOKE ALL ON FUNCTION fn_stock_movements_ledger() FROM PUBLIC
""",
    """
DROP TRIGGER IF EXISTS trg_products_stock_ledger ON products
""",
    """
CREATE TRIGGER trg_products_stock_ledger
    AFTER INSERT OR UPDATE OF stock ON products
    FOR EACH ROW
    EXECUTE FUNCTION fn_stock_movements_ledger()
""",
    # 5) Views de consulta -----------------------------------------------------
    """
CREATE OR REPLACE VIEW vw_estoque_atual AS
SELECT
    p.id                         AS produto_id,
    p.name                       AS sabor,
    p.category                   AS categoria,
    p.stock                      AS estoque_fisico,
    p.reserved_stock             AS reservado_pix,
    p.stock - p.reserved_stock   AS disponivel_marketplace,
    ultima.created_at            AS ultima_movimentacao
FROM products AS p
LEFT JOIN LATERAL (
    SELECT m.created_at
      FROM stock_movements AS m
     WHERE m.product_id = p.id
     ORDER BY m.created_at DESC, m.id DESC
     LIMIT 1
) AS ultima ON true
""",
    """
CREATE OR REPLACE VIEW vw_movimentacoes_estoque AS
SELECT
    m.id,
    m.created_at                                  AS horario_utc,
    (m.created_at AT TIME ZONE 'America/Recife')  AS horario_recife,
    m.product_id                                  AS produto_id,
    m.product_name                                AS sabor,
    m.movement_type                               AS tipo,
    CASE m.movement_type
        WHEN 'INITIAL_STOCK'    THEN 'Estoque inicial'
        WHEN 'RESTOCK'          THEN 'Reposição'
        WHEN 'MANUAL_SALE'      THEN 'Venda manual'
        WHEN 'LOSS'             THEN 'Perda/avaria'
        WHEN 'ADJUSTMENT'       THEN 'Ajuste de inventário'
        WHEN 'MARKETPLACE_SALE' THEN 'Venda no marketplace'
        ELSE 'Alteração sem contexto'
    END                                           AS tipo_descricao,
    m.source                                      AS origem,
    CASE WHEN m.delta < 0 THEN 'SAIDA' ELSE 'ENTRADA' END AS direcao,
    m.quantity                                    AS quantidade,
    m.delta,
    m.stock_before                                AS estoque_antes,
    m.stock_after                                 AS estoque_depois,
    m.buyer_name                                  AS comprador,
    m.buyer_email                                 AS email_comprador,
    m.order_id                                    AS pedido_id,
    o.paid_at                                     AS pago_em,
    m.unit_price                                  AS preco_unitario,
    m.unit_price * m.quantity                     AS valor_total,
    su.username                                   AS operador_usuario,
    su.full_name                                  AS operador_nome,
    m.note                                        AS observacao,
    m.db_user                                     AS usuario_banco
FROM stock_movements AS m
LEFT JOIN orders AS o ON o.id = m.order_id
LEFT JOIN staff_users AS su ON su.id = m.staff_user_id
""",
    # Todas as reduções de estoque. Para vendas, filtre por tipo
    # (MARKETPLACE_SALE / MANUAL_SALE); ajustes negativos também aparecem aqui.
    """
CREATE OR REPLACE VIEW vw_saidas_estoque AS
SELECT *
  FROM vw_movimentacoes_estoque
 WHERE delta < 0
""",
)


DOWNGRADE_STATEMENTS: tuple[str, ...] = (
    "DROP VIEW IF EXISTS vw_saidas_estoque",
    "DROP VIEW IF EXISTS vw_movimentacoes_estoque",
    "DROP VIEW IF EXISTS vw_estoque_atual",
    "DROP TRIGGER IF EXISTS trg_products_stock_ledger ON products",
    "DROP FUNCTION IF EXISTS fn_stock_movements_ledger()",
    "DROP TABLE IF EXISTS stock_movements",
    "DROP TABLE IF EXISTS staff_sessions",
    "DROP TABLE IF EXISTS staff_users",
)


def upgrade() -> None:
    for statement in UPGRADE_STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE_STATEMENTS:
        op.execute(statement)
