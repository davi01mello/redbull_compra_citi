# Red Bull PDV — MVP

Esqueleto funcional do autoatendimento descrito em `AGENTS.md`. Produtos, pedidos e estoque são persistidos no PostgreSQL. O pagamento PIX ainda é simulado e nenhuma cobrança real é criada.

## Executar

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
docker compose up -d postgres
alembic upgrade head
uvicorn app.main:app --reload
```

Acesse `http://127.0.0.1:8000`. A documentação da API fica em `http://127.0.0.1:8000/docs`.

## Fluxos disponíveis

- PDV mobile: nome, sabores, quantidades, resumo e criação do pedido.
- PIX simulado: QR ilustrativo, copia e cola e botão para aprovar localmente.
- Webhook protegido por token e idempotente (`X-Webhook-Token: mvp-local-token`).

## Testes

Os testes usam exclusivamente `TEST_DATABASE_URL`. Essa URL deve apontar para um PostgreSQL cujo nome comece com `test` ou termine com `_test`, e não pode ser a mesma de `DATABASE_URL`.

```bash
docker compose up -d postgres_test
pytest
```

## Próximas integrações

As rotas `/api/mock/*` são exclusivas do MVP e deverão ser removidas quando um gateway PIX implementar a criação da cobrança e o webhook assinado.
