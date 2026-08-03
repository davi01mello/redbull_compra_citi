# Red Bull PDV — MVP

Esqueleto funcional do autoatendimento descritos em `AGENTS.md`. Nesta etapa, os dados vivem apenas na memória do processo e o pagamento PIX é simulado: nada é persistido e nenhuma cobrança real é criada.

## Executar

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Acesse `http://127.0.0.1:8000`. A documentação da API fica em `http://127.0.0.1:8000/docs`.

## Fluxos disponíveis

- PDV mobile: nome, sabores, quantidades, resumo e criação do pedido.
- PIX simulado: QR ilustrativo, copia e cola e botão para aprovar localmente.
- Webhook protegido por token e idempotente (`X-Webhook-Token: mvp-local-token`).

## Testes

```bash
pytest
```

## Próximas integrações

`MemoryStore` concentra o contrato temporário de persistência. Ele deve ser substituído por um repositório transacional conectado ao banco. As rotas `/api/mock/*` são exclusivas do MVP e deverão ser removidas quando um gateway PIX implementar a criação da cobrança e o webhook assinado.
