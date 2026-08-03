# Contexto e Arquitetura do Sistema: Gestão de Vendas Red Bull (CITi)

## 1. Visão Geral do Projeto
Sistema automatizado de autoatendimento (PDV mobile) e gestão de estoque para vendas internas. O fluxo de compra é iniciado via QR Code físico e concluído sem intervenção humana. O sistema utiliza uma arquitetura baseada em PIX dinâmico e Webhooks para validação automática de pagamentos, eliminando a necessidade de envio e conferência manual de comprovantes.

## 2. Stack Tecnológica (Sugerida/Base)
- **Backend:** Python com FastAPI (foco em requisições assíncronas e webhooks).
- **Frontend (PDV):** Interface web mobile-first.
- **Integração Financeira:** Gateway de pagamento via API (ex: Mercado Pago, Efí, Asaas) para geração de PIX e recepção de webhooks.

## 3. Atores do Sistema
- **Comprador (Membro/Não Membro):** Acessa a interface mobile pelo QR Code, seleciona produtos e realiza o pagamento.
- **Gateway de Pagamento:** Sistema externo que processa o PIX e notifica o backend via Webhook.

## 4. Fluxo de Processos (Cenário 2 - 100% Automatizado)

### Processo A: Ponto de Venda e Geração do Pedido
1. O Comprador escaneia um QR Code físico colado no local de armazenamento que o redireciona para o Web App (Mobile).
2. O Comprador informa seu nome, seleciona o sabor (ex: Tropical, Melancia, Energy Original) e a quantidade desejada.
3. O Frontend calcula o subtotal e envia o *payload* (nome, itens, total) para o endpoint de criação de pedido no Backend (FastAPI).

### Processo B: Comunicação Financeira (PIX Dinâmico)
1. O Backend recebe o pedido e o salva no Banco de Dados com o status `PENDING`.
2. Imediatamente, o Backend faz uma requisição HTTP para a API do Gateway de Pagamento solicitando uma cobrança PIX no valor exato.
3. A API externa retorna o payload do PIX (QR Code e string "Copia e Cola") atrelado a um identificador único de transação (`txid`).
4. O Backend associa este `txid` ao pedido no Banco de Dados e devolve as credenciais de pagamento para o Frontend exibir ao Comprador.

### Processo C: Conciliação Automática (Webhooks) - *Core Business*
1. O Comprador efetua o pagamento no aplicativo do seu banco utilizando o PIX gerado.
2. O Gateway de Pagamento identifica o recebimento e dispara uma requisição `POST` para a rota de Webhook do Backend (ex: `/api/webhooks/pix`).
3. O Backend recebe o *payload* do Webhook, extrai o `txid` e busca o pedido correspondente no Banco de Dados.
4. O Backend executa duas ações transacionais em lote:
   - Altera o status do pedido de `PENDING` para `PAID` (Confirmada).
   - Subtrai a quantidade vendida do estoque do sabor específico.


## 5. Regras de Negócio e Restrições para a IA
- **Transações:** A atualização de estoque e a mudança de status do pedido no Webhook devem ser atômicas (ocorrer na mesma transação de banco de dados).
- **Segurança do Webhook:** A rota `/api/webhooks/pix` deve validar a assinatura/token do Gateway de Pagamento para evitar que usuários maliciosos disparem confirmações falsas de pagamento.
- **Idempotência:** O processamento do Webhook deve ser idempotente (se o Gateway enviar o aviso de pagamento do mesmo `txid` duas vezes, o estoque só deve ser subtraído uma vez).
