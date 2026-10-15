# Red Bull PDV

Autoatendimento mobile para venda de Red Bull com reserva de estoque e pagamento Pix. O sistema suporta cobranças reais pelo Mercado Pago e mantém o provedor mock somente para desenvolvimento.

## Fluxo de compra

O comprador seleciona os produtos, informa nome e e-mail, confirma a identificação e então recebe o QR Code Pix. O e-mail é validado no navegador e no backend, normalizado e persistido em `orders.payer_email`; registros antigos podem continuar com `NULL`. A criação da Order usa exclusivamente o e-mail persistido no pedido em `payer.email`.

O e-mail do pagador não é uma variável de ambiente. Não configure `MERCADO_PAYER_EMAIL` nem `MERCADOPAGO_PAYER_EMAIL`: cada comprador informa o próprio e-mail no checkout.

Após o pagamento, o webhook assinado consulta `GET /v1/orders/{provider_order_id}`. A confirmação altera pedido, pagamento, reserva e estoque na mesma transação, com locks de linha e verificações de estado para impedir desconto duplicado. Uma reconciliação periódica consulta pagamentos pendentes antes da expiração, cobrindo webhooks perdidos ou consultas precoces.

Nunca corrija somente `orders.status` nem desconte estoque diretamente por SQL. Use o comando de reconciliação, que reutiliza a transação central. Entradas, saídas manuais e ajustes de estoque são feitos pelo painel `/estoque`.

## Configuração

Produção no Render deve usar variáveis de ambiente, sem valores no Git:

```env
DATABASE_URL=
PAYMENT_PROVIDER=mercadopago
MERCADOPAGO_ACCESS_TOKEN=
MERCADOPAGO_WEBHOOK_SECRET=
CHARGE_EXPIRES_IN=1800
PAYMENT_CHECKOUT_EXPIRES_IN=600
PAYMENT_EXPIRATION_CHECK_INTERVAL=5
```

O webhook deve ser cadastrado como `POST /api/webhooks/mercadopago`. A aplicação, o segredo do webhook e o Access Token precisam pertencer à mesma integração/conta do Mercado Pago. Não registre tokens, assinaturas, QR Code, código copia e cola ou e-mail completo em logs.

Para desenvolvimento local, use `PAYMENT_PROVIDER=mock`; as rotas `/api/mock/*` respondem 404 quando o Mercado Pago está ativo.

## Execução local

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
docker compose up -d postgres
alembic upgrade head
uvicorn app.main:app --reload
```

A aplicação fica em `http://127.0.0.1:8000` e a documentação em `/docs`.

## Controle de estoque (`/estoque`)

Painel com login para operadores registrarem entradas e saídas de Red Bull. Ele lista todos os sabores cadastrados em `products` (os mesmos do marketplace), mostra estoque físico, reservas PIX abertas e o que está disponível no PDV, e tem os botões **− Saída** (venda manual ou perda) e **+ Entrada** (reposição). Administradores também podem lançar um **ajuste de inventário** (contagem física).

Toda alteração de `products.stock` fica registrada em `stock_movements` pelo trigger `trg_products_stock_ledger`, inclusive as vendas pagas no marketplace: quem comprou (nome e e-mail), quantas latas saíram, horário, pedido, preço unitário e operador. Alterações feitas direto por SQL também são registradas, como `UNTRACKED`. Use o painel para repor ou corrigir estoque; não faça `UPDATE products SET stock` manual.

Primeiro acesso:

```bash
alembic upgrade head                      # cria staff_users, staff_sessions, stock_movements, trigger e views
psql "$URL_DO_BANCO" -f sql/seed_sabores.sql   # opcional: cadastra os 12 sabores com estoque 0
python -m scripts.create_staff_user --username seu.usuario --name "Seu Nome" --role ADMIN
```

Depois acesse `http://127.0.0.1:8000/estoque`. Em desenvolvimento sem HTTPS, defina `STOCK_SESSION_COOKIE_SECURE=false`.

- Modelo de dados em SQL puro: `sql/estoque_schema.sql` (idêntico à revisão `c3f8a1d27b54`).
- Relatórios prontos (saídas do dia, quem comprou, ranking de sabores, integridade): `sql/consultas_estoque.sql`.
- Plano de ação e fluxo completo: `docs/plano-acao-estoque.md`.

## Alembic e banco institucional

Na auditoria de 2026-08-11, o banco institucional estava em `e91a7c4d2f30`; o histórico local anterior tinha `a6c42f1d9b73` como head. O banco também contém `users` e `stock_adjustments`, tabelas externas ao ORM/histórico deste serviço, que devem ser preservadas. As colunas históricas `products.description`, `products.color` e `products.price`, presentes em instalações criadas do zero mas ausentes no banco institucional, também são preservadas. Esses objetos estão marcados como não gerenciados no autogenerate para impedir propostas destrutivas.

A revisão `4c7b91e2a6d0` parte do estado remoto `e91a7c4d2f30`, adiciona `orders.payer_email` como anulável e só cria `products.category` quando ausente. A revisão `7e03d1bf5a42` une essa linha à revisão de preço `a6c42f1d9b73`. Nenhuma migration foi aplicada em produção durante esta implementação.

Ordem segura de implantação:

1. confirmar `alembic current`, `alembic heads` e o schema real;
2. confirmar backup/ponto de restauração;
3. testar upgrade e downgrade em cópia ou banco de teste;
4. revisar que `users` e `stock_adjustments` não serão alteradas;
5. aplicar a migration aprovada;
6. implantar backend e frontend;
7. executar uma compra controlada e validar pagamento/reserva/estoque.

Não use `alembic stamp head` e não aplique a migration no banco institucional sem autorização explícita.

## Reparo controlado

Simulação por ID interno:

```bash
python -m scripts.reconcile_payment --order-id UUID_DO_PEDIDO --dry-run
```

Simulação por ID do Mercado Pago:

```bash
python -m scripts.reconcile_payment --provider-order-id ID_DA_ORDER --dry-run
```

Depois de revisar o status e a compatibilidade do valor, remova `--dry-run` para executar. O evento usa uma chave idempotente derivada de `manual-reconciliation:{provider_order_id}` e a mesma função transacional do webhook.

As consultas de leitura para pagamentos pendentes e consistência das reservas estão em [`docs/payment_diagnostics.sql`](docs/payment_diagnostics.sql).

O inventário dos arquivos adicionados, as decisões de implementação e os procedimentos operacionais estão em [`docs/alteracoes-recentes.md`](docs/alteracoes-recentes.md).

## Testes

Os testes usam exclusivamente `TEST_DATABASE_URL`. O nome do banco precisa começar com `test` ou terminar com `_test`, e a URL não pode ser igual a `DATABASE_URL`.

```bash
docker compose up -d postgres_test
pytest
```
