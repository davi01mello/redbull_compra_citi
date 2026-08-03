# Red Bull PDV — CITi

Autoatendimento mobile para venda interna de Red Bull com pedido, reserva de estoque e pagamento PIX. O escopo atual é exclusivamente o checkout; não há dashboard administrativo.

## Executar localmente

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
uvicorn app.main:app --reload
```

- PDV: http://127.0.0.1:8000
- API: http://127.0.0.1:8000/docs

## Modos de pagamento

### Demonstração

```dotenv
PAYMENT_PROVIDER=mock
```

Gera um QR ilustrativo e permite simular a confirmação. Não movimenta dinheiro.

### PIX pessoal temporário

```dotenv
PAYMENT_PROVIDER=static_pix
PIX_KEY=sua-chave-pix
PIX_RECEIVER_NAME=SEU NOME
PIX_RECEIVER_CITY=Fortaleza
```

O sistema gera um BR Code PIX real com o valor exato do pedido. O dinheiro é enviado para a chave configurada. Esse modo não possui webhook e, portanto, não confirma o recebimento automaticamente. Ele é destinado apenas à validação temporária da etapa de pagamento.

### Woovi/OpenPix

```dotenv
PAYMENT_PROVIDER=openpix
OPENPIX_ENVIRONMENT=sandbox
OPENPIX_APP_ID=<appID>
OPENPIX_WEBHOOK_SECRET=<secret HMAC>
```

Este é o modo definitivo: cria uma cobrança dinâmica, recebe o webhook, consulta novamente a cobrança na Woovi e atualiza pedido e estoque uma única vez. O webhook público deve ser:

```text
https://<seu-servico>.onrender.com/api/webhooks/openpix
```

Ative os eventos `OPENPIX:CHARGE_COMPLETED` e `OPENPIX:CHARGE_EXPIRED`. Nunca envie credenciais por mensagem nem faça commit do `.env`.

## Testes

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```
