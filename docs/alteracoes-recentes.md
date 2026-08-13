# Documentação das alterações recentes

Este documento descreve os arquivos adicionados e alterados em 11 de agosto de 2026. A implementação identifica o pagador pelo e-mail informado no checkout, torna a confirmação de pagamentos Mercado Pago mais resiliente e permite diagnóstico ou reparo controlado sem alterar estados diretamente por SQL.

## Resumo funcional

O fluxo de compra agora é:

1. o comprador seleciona os produtos e informa o nome;
2. uma etapa intermediária solicita e valida o e-mail;
3. o backend normaliza e salva o e-mail no pedido;
4. a cobrança Pix é criada com o e-mail persistido em `payer.email`;
5. o webhook assinado ou a reconciliação periódica consulta a Order no Mercado Pago;
6. a transação central atualiza pedido, pagamento, reserva e estoque de forma atômica e idempotente.

Pedidos anteriores continuam legíveis porque `orders.payer_email` aceita `NULL`. Novos pedidos exigem um e-mail válido.

## Variáveis de ambiente

O e-mail do pagador não é uma configuração global. Não adicione `MERCADO_PAYER_EMAIL`, `MERCADOPAGO_PAYER_EMAIL` ou outra variável equivalente ao ambiente. Cada comprador informa o próprio e-mail no checkout e esse valor fica associado ao pedido.

Para o Mercado Pago, permanecem necessárias as credenciais da integração:

```env
PAYMENT_PROVIDER=mercadopago
MERCADOPAGO_ACCESS_TOKEN=
MERCADOPAGO_WEBHOOK_SECRET=
```

Também continuam válidas as configurações de expiração já descritas no `README.md`. Variáveis antigas de e-mail fixo ou de teste foram removidas para impedir que um valor global substitua a identificação individual.

## Arquivos adicionados

| Arquivo | Responsabilidade |
| --- | --- |
| `app/db/migrations/versions/4c7b91e2a6d0_add_payer_email_to_orders.py` | Adiciona `orders.payer_email` e alinha `products.category` somente quando a coluna não existe. |
| `app/db/migrations/versions/7e03d1bf5a42_merge_payer_email_and_price_heads.py` | Une a migration de e-mail à linha de alteração de preços, deixando um único head no Alembic. |
| `scripts/__init__.py` | Torna `scripts` um pacote Python executável com `python -m`. |
| `scripts/reconcile_payment.py` | Oferece reconciliação manual, por ID interno ou ID do Mercado Pago, com modo de simulação. |
| `docs/payment_diagnostics.sql` | Reúne consultas somente de leitura para pagamentos pendentes e consistência das reservas. |
| `tests/test_frontend_email.py` | Verifica estrutura acessível, validação e comportamento da nova etapa de e-mail. |
| `tests/test_migrations.py` | Verifica o head do Alembic, o formato da coluna de e-mail e o downgrade não destrutivo. |
| `tests/test_payment_reconciliation.py` | Verifica reconciliação periódica/manual, falhas parciais, estados terminais e concorrência. |

## Arquivos de suporte alterados

### Checkout e persistência do e-mail

- `app/static/index.html` adiciona a tela de identificação entre o carrinho e o Pix, com rótulo, mensagens acessíveis, aviso de privacidade e navegação de retorno.
- `app/static/app.js` normaliza o e-mail com `trim()` e conversão para minúsculas, valida o formato, bloqueia submissões duplicadas e preserva o valor digitado quando a API falha.
- `app/static/styles.css` inclui os estados visual, inválido e de carregamento da nova tela.
- `app/models.py` exige `payer_email` em novos pedidos, limita-o a 254 caracteres e repete a validação no backend. O nome do comprador também é normalizado.
- `app/db/models.py` mapeia a nova coluna anulável em `OrderORM`.
- `app/store.py` persiste o nome e o e-mail já normalizados pelo modelo de entrada.

O e-mail não é exposto no modelo de resposta `Order`. Ele existe para identificar a cobrança e é enviado ao Mercado Pago exclusivamente a partir do pedido salvo.

### Integração Mercado Pago

- `app/payments.py` passou a exigir `payer_email` no contrato `create_charge` e envia esse valor em `payer.email` na criação da Order.
- A consulta `GET /v1/orders/{provider_order_id}` interpreta aprovação, cancelamento, expiração, valor e instante da aprovação.
- Os erros HTTP são reduzidos a metadados operacionais. E-mails e tokens Bearer encontrados em mensagens são mascarados antes do log.
- `app/payment_service.py` usa o e-mail salvo ao criar a cobrança e rejeita pedidos sem esse dado, sem recorrer a um valor alternativo.

### Webhook

`POST /api/webhooks/mercadopago` usa `data.id` da query string como ID da Order e como parte da validação da assinatura. O corpo é opcional e serve somente para metadados adicionais. Depois de validar a assinatura, o backend consulta a Order no Mercado Pago; o estado enviado no corpo não é tratado como fonte de verdade.

O processamento central:

- confere se o ID remoto corresponde ao ID notificado;
- localiza o pagamento pelo provedor e `provider_order_id`;
- confere o valor remoto contra o valor local;
- bloqueia pedido, pagamento e produtos durante a mudança de estado;
- atualiza pagamento, pedido, reserva e estoque na mesma transação;
- registra o evento processado;
- reconhece estados já processados sem descontar o estoque novamente;
- encaminha inconsistências para revisão manual interna.

A rota pública `GET /api/manual-reviews` foi removida. Os registros continuam no banco para investigação operacional, sem exposição por essa API.

## Reconciliação de pagamentos

A reconciliação cobre situações em que o webhook não chegou, chegou cedo demais ou falhou temporariamente.

### Reconciliação periódica

Antes de expirar cobranças, a rotina consulta no Mercado Pago pagamentos locais nos estados `CREATING` e `WAITING_PAYMENT` que possuem `provider_order_id`. Cada resultado remoto é encaminhado para a mesma função transacional usada pelo webhook.

- Pagamentos aprovados consomem a reserva e descontam o estoque uma vez.
- Pagamentos ainda pendentes permanecem sem alteração.
- Pagamentos cancelados ou expirados remotamente liberam a reserva.
- Uma falha de consulta fica isolada e não impede o restante do lote.

Essa ordem evita cancelar ou expirar localmente um pagamento que já foi aprovado no provedor. Pagamentos sem `provider_order_id` ou com falha de comunicação não são expirados automaticamente naquela passagem.

### Reconciliação manual

O comando exige `PAYMENT_PROVIDER=mercadopago` e as credenciais normais da aplicação. Execute primeiro em modo de simulação:

```bash
python -m scripts.reconcile_payment --order-id UUID_DO_PEDIDO --dry-run
```

Também é possível localizar pelo ID remoto:

```bash
python -m scripts.reconcile_payment \
  --provider-order-id ID_DA_ORDER_NO_MERCADO_PAGO \
  --dry-run
```

O comando mostra os estados locais, o estado remoto e se o valor coincide. Se houver divergência de valor, nenhuma alteração é permitida. Depois da conferência, remova `--dry-run` para aplicar:

```bash
python -m scripts.reconcile_payment --order-id UUID_DO_PEDIDO
```

A execução real usa uma chave de evento derivada de `manual-reconciliation:{provider_order_id}` e chama o processamento transacional central. Repetir o comando não repete o desconto de estoque.

Não altere `orders.status`, `payments.status`, `reservation_status`, `stock` ou `reserved_stock` diretamente para corrigir um pagamento.

## Migrações e compatibilidade do banco

A migration `4c7b91e2a6d0` parte da revisão `e91a7c4d2f30`, encontrada no banco institucional durante a auditoria. Ela:

- cria `products.category` apenas se a coluna estiver ausente;
- cria `orders.payer_email` como `VARCHAR(254) NULL`;
- no downgrade, remove somente `orders.payer_email`;
- preserva `products.category` no downgrade porque a coluna já existia no banco institucional.

A migration `7e03d1bf5a42` une `4c7b91e2a6d0` e `a6c42f1d9b73`, sem executar alterações adicionais. O resultado esperado de `alembic heads` é um único head: `7e03d1bf5a42`.

O filtro em `app/db/migrations/env.py` exclui do autogenerate:

- tabelas externas ao serviço: `users` e `stock_adjustments`;
- colunas históricas não gerenciadas de `products`: `description`, `color` e `price`.

Isso impede que uma futura geração automática proponha a remoção desses objetos. O filtro não substitui backup nem revisão manual da migration.

### Implantação segura

1. execute `alembic current` e `alembic heads`;
2. confirme o schema real e um backup ou ponto de restauração;
3. teste upgrade e downgrade em uma cópia ou banco de teste;
4. confirme que os objetos externos não serão alterados;
5. aplique `alembic upgrade head` somente após aprovação;
6. implante backend e frontend;
7. realize uma compra controlada e confira pedido, pagamento, reserva e estoque.

Não use `alembic stamp head` para contornar divergências e não aplique a migration no banco institucional sem autorização.

## Diagnóstico SQL

O arquivo `docs/payment_diagnostics.sql` contém duas consultas somente de leitura:

1. lista pagamentos Mercado Pago ainda pendentes localmente, incluindo o último evento conhecido;
2. compara `products.reserved_stock` com a soma esperada dos itens em reservas ativas.

Na segunda consulta, `difference = 0` indica consistência. Um valor diferente de zero deve ser investigado; a consulta não faz correções.

## Cobertura de testes adicionada

| Área | Garantias verificadas |
| --- | --- |
| Frontend | Ordem das telas, acessibilidade, validação, normalização, prevenção de envio duplicado e preservação do formulário em falhas. |
| API e modelo | E-mail obrigatório para pedidos novos, rejeição de formatos inválidos, persistência sem exposição e leitura de pedidos antigos com `NULL`. |
| Mercado Pago | Uso do e-mail do pedido, payload da cobrança, consulta da Order, assinatura, cancelamento idempotente e logs sem dados sensíveis. |
| Webhook | Corpo vazio, ausência de `data.id`, assinatura inválida, duplicidade e webhooks concorrentes. |
| Reconciliação | Aprovação, pendência, estados terminais, falha isolada, comando manual e concorrência sem duplo desconto. |
| Alembic | Head único, coluna anulável com 254 caracteres e downgrade restrito à coluna do serviço. |

Para executar a suíte:

```bash
docker compose up -d postgres_test
pytest
```

Os testes usam somente `TEST_DATABASE_URL`. O nome do banco deve começar com `test` ou terminar com `_test`, e a URL não pode ser a mesma de `DATABASE_URL`.
