-- =====================================================================
-- Consultas de estoque (somente leitura) — Red Bull CITi
-- Horários exibidos no fuso America/Recife.
-- Rode trechos individuais no psql, DBeaver ou pgAdmin.
-- =====================================================================

-- 1) Estoque atual por sabor --------------------------------------------
SELECT produto_id, sabor, categoria,
       estoque_fisico, reservado_pix, disponivel_marketplace,
       ultima_movimentacao AT TIME ZONE 'America/Recife' AS ultima_movimentacao
  FROM vw_estoque_atual
 ORDER BY categoria, sabor;


-- Observação: vw_saidas_estoque traz toda redução de estoque. As consultas
-- abaixo filtram por tipo para contar só saídas reais de latas (vendas e
-- perdas); ajustes de inventário ficam de fora dos totais.

-- 2) Saídas de hoje: quem comprou, quantas latas e a que horas -------------
SELECT to_char(horario_recife, 'HH24:MI') AS hora,
       sabor, quantidade, tipo_descricao, comprador, email_comprador,
       valor_total, operador_nome, observacao, pedido_id
  FROM vw_saidas_estoque
 WHERE horario_recife >= date_trunc('day', now() AT TIME ZONE 'America/Recife')
   AND tipo IN ('MARKETPLACE_SALE', 'MANUAL_SALE', 'LOSS')
 ORDER BY horario_recife DESC;


-- 3) Saídas em um período (ajuste as datas) ------------------------------
SELECT horario_recife, sabor, quantidade, tipo_descricao, comprador,
       email_comprador, preco_unitario, valor_total, operador_nome, observacao
  FROM vw_saidas_estoque
 WHERE horario_recife >= DATE '2026-10-01'
   AND horario_recife <  DATE '2026-11-01'
   AND tipo IN ('MARKETPLACE_SALE', 'MANUAL_SALE', 'LOSS')
 ORDER BY horario_recife;


-- 4) Sabores que mais saíram nos últimos 30 dias ------------------------
SELECT sabor,
       sum(quantidade)                                            AS latas,
       sum(quantidade) FILTER (WHERE tipo = 'MARKETPLACE_SALE')   AS marketplace,
       sum(quantidade) FILTER (WHERE tipo = 'MANUAL_SALE')        AS venda_manual,
       sum(quantidade) FILTER (WHERE tipo = 'LOSS')               AS perdas,
       sum(valor_total)                                           AS faturamento_marketplace
  FROM vw_saidas_estoque
 WHERE horario_utc >= now() - interval '30 days'
   AND tipo IN ('MARKETPLACE_SALE', 'MANUAL_SALE', 'LOSS')
 GROUP BY sabor
 ORDER BY latas DESC;


-- 5) Compras por pessoa (marketplace + venda manual) --------------------
SELECT coalesce(email_comprador, lower(comprador))  AS comprador_chave,
       max(comprador)                               AS comprador,
       count(DISTINCT coalesce(pedido_id::text, id::text)) AS compras,
       sum(quantidade)                              AS latas,
       max(horario_recife)                          AS ultima_compra
  FROM vw_saidas_estoque
 WHERE tipo IN ('MARKETPLACE_SALE', 'MANUAL_SALE')
 GROUP BY 1
 ORDER BY latas DESC;


-- 6) Saídas por dia ------------------------------------------------------
SELECT horario_recife::date AS dia,
       sum(quantidade) AS latas,
       sum(quantidade) FILTER (WHERE tipo = 'MARKETPLACE_SALE') AS marketplace,
       sum(quantidade) FILTER (WHERE tipo = 'MANUAL_SALE')      AS manual,
       sum(quantidade) FILTER (WHERE tipo = 'LOSS')             AS perdas
  FROM vw_saidas_estoque
 WHERE tipo IN ('MARKETPLACE_SALE', 'MANUAL_SALE', 'LOSS')
 GROUP BY 1
 ORDER BY 1 DESC;


-- 7) Lançamentos por operador do painel ----------------------------------
SELECT operador_nome, tipo_descricao, count(*) AS lancamentos, sum(quantidade) AS latas
  FROM vw_movimentacoes_estoque
 WHERE origem = 'PAINEL'
 GROUP BY operador_nome, tipo_descricao
 ORDER BY operador_nome, tipo_descricao;


-- 8) Alterações feitas fora do sistema (investigar) -----------------------
-- Toda mudança em products.stock sem contexto da aplicação (ex.: UPDATE
-- manual) aparece como UNTRACKED, com o usuário do banco que a executou.
SELECT horario_recife, sabor, delta, estoque_antes, estoque_depois,
       usuario_banco, observacao
  FROM vw_movimentacoes_estoque
 WHERE tipo = 'UNTRACKED'
 ORDER BY horario_recife DESC;


-- 9) Conferência de integridade: estoque x livro-razão --------------------
-- Para cada sabor com histórico, o estoque_depois da última movimentação
-- deve ser igual ao estoque atual. "diferenca" diferente de zero indica
-- alteração que escapou do trigger (não deveria acontecer).
SELECT p.id, p.name, p.stock AS estoque_atual,
       ultima.stock_after AS estoque_no_razao,
       p.stock - ultima.stock_after AS diferenca
  FROM products AS p
  JOIN LATERAL (
        SELECT m.stock_after
          FROM stock_movements AS m
         WHERE m.product_id = p.id
         ORDER BY m.id DESC
         LIMIT 1
  ) AS ultima ON true
 ORDER BY abs(p.stock - ultima.stock_after) DESC, p.name;


-- 10) Tabelas legadas do controle de estoque antigo ----------------------
-- users e stock_adjustments vieram do sistema perdido e são preservadas.
-- Existem só no banco institucional (numa instalação nova a 2ª consulta
-- falha com "relation does not exist", o que é esperado).
-- Use estas consultas para decidir se vale importar o histórico.
SELECT table_name, column_name, data_type, is_nullable
  FROM information_schema.columns
 WHERE table_schema = current_schema()
   AND table_name IN ('users', 'stock_adjustments')
 ORDER BY table_name, ordinal_position;

SELECT 'users' AS tabela, count(*) AS linhas FROM users
UNION ALL
SELECT 'stock_adjustments', count(*) FROM stock_adjustments;
