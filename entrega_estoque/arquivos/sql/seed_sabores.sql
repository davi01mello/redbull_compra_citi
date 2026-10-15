-- =====================================================================
-- Catálogo de sabores Red Bull do marketplace CITi (OPCIONAL)
--
-- Cadastra os 12 sabores que o PDV já sabe exibir (imagens em
-- app/static/assets). Rode só depois de `alembic upgrade head`.
--
-- Seguro para rodar mais de uma vez e no banco institucional:
--   * não altera sabores existentes (nem estoque, nem nome);
--   * pula o sabor se já existir um produto com o mesmo id OU com o
--     mesmo nome normalizado (sem acento/maiúsculas/espaços), evitando
--     duplicar um sabor cadastrado com outro id;
--   * todos entram com estoque 0 — use o painel /estoque (botão
--     "+ Entrada") para lançar as latas, assim a entrada fica registrada
--     com o operador responsável;
--   * funciona com ou sem as colunas legadas products.description,
--     products.color e products.price.
--
-- Uso: psql "$DATABASE_URL_SEM_ASYNCPG" -f sql/seed_sabores.sql
-- =====================================================================

BEGIN;

DO $seed$
DECLARE
    v_has_legacy boolean;
    v_flavor     record;
    v_inserted   integer := 0;
    v_skipped    integer := 0;
BEGIN
    SELECT count(*) = 3
      INTO v_has_legacy
      FROM information_schema.columns
     WHERE table_schema = current_schema()
       AND table_name = 'products'
       AND column_name IN ('description', 'color', 'price');

    FOR v_flavor IN
        SELECT *
          FROM (VALUES
            ('original',              'Energy Original',        'Regular',   'O clássico que dá asas.',          '#164194'),
            ('tropical',              'Tropical',               'Regular',   'Sabor tropical e refrescante.',    '#f5c400'),
            ('melancia',              'Melancia',               'Regular',   'Leve, frutado e marcante.',        '#e7354f'),
            ('nectarina',             'Nectarina',              'Regular',   'Edição de verão de nectarina.',    '#f28c28'),
            ('melao_e_maracuja',      'Melão e Maracujá',       'Regular',   'Melão com maracujá.',              '#7cc242'),
            ('morango_e_pessego',     'Morango e Pêssego',      'Regular',   'Morango com pêssego.',             '#e8506e'),
            ('zero_tradicional',      'Tradicional Zero',       'Zero',      'Zero açúcar e zero calorias.',     '#9aa3ad'),
            ('tradicional_sugarfree', 'Tradicional Sugarfree',  'Sugarfree', 'Sem açúcar.',                      '#4fb3e8'),
            ('nectarina_sugarfree',   'Nectarina Sugarfree',    'Sugarfree', 'Nectarina sem açúcar.',            '#f5a25d'),
            ('pomelo_sugarfree',      'Pomelo Sugarfree',       'Sugarfree', 'Pomelo sem açúcar.',               '#f07c8c'),
            ('amora_sugarfree',       'Amora Sugarfree',        'Sugarfree', 'Amora sem açúcar.',                '#7b4fa0'),
            ('maca_sugarfree',        'Maçã Sugarfree',         'Sugarfree', 'Maçã sem açúcar.',                 '#8cc63f')
          ) AS f(id, name, category, description, color)
    LOOP
        IF EXISTS (
            SELECT 1
              FROM products AS p
             WHERE p.id = v_flavor.id
                OR regexp_replace(
                       lower(translate(p.name, 'áàâãäéèêëíìîïóòôõöúùûüçÁÀÂÃÄÉÈÊËÍÌÎÏÓÒÔÕÖÚÙÛÜÇ',
                                               'aaaaaeeeeiiiiooooouuuucaaaaaeeeeiiiiooooouuuuc')),
                       '[^a-z0-9]+', '', 'g')
                 = regexp_replace(
                       lower(translate(v_flavor.name, 'áàâãäéèêëíìîïóòôõöúùûüçÁÀÂÃÄÉÈÊËÍÌÎÏÓÒÔÕÖÚÙÛÜÇ',
                                                      'aaaaaeeeeiiiiooooouuuucaaaaaeeeeiiiiooooouuuuc')),
                       '[^a-z0-9]+', '', 'g')
        ) THEN
            v_skipped := v_skipped + 1;
            CONTINUE;
        END IF;

        IF v_has_legacy THEN
            -- Instalações criadas do zero ainda têm as colunas legadas NOT NULL.
            INSERT INTO products (id, name, category, stock, reserved_stock, description, color, price)
            VALUES (v_flavor.id, v_flavor.name, v_flavor.category, 0, 0,
                    v_flavor.description, v_flavor.color, 8.50);
        ELSE
            INSERT INTO products (id, name, category, stock, reserved_stock)
            VALUES (v_flavor.id, v_flavor.name, v_flavor.category, 0, 0);
        END IF;
        v_inserted := v_inserted + 1;
    END LOOP;

    RAISE NOTICE 'Sabores cadastrados: %, já existentes (ignorados): %', v_inserted, v_skipped;
END
$seed$;

COMMIT;

-- Conferência
SELECT produto_id, sabor, categoria, estoque_fisico, disponivel_marketplace
  FROM vw_estoque_atual
 ORDER BY categoria, sabor;
