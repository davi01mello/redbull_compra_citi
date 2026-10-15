# Entrega — Controle de estoque Red Bull (CITi)

Base: `main` do GitHub, commit `0feecef` (Merge PR #7 `fix/bd`), head Alembic `7e03d1bf5a42`.
Sua pasta local está na branch `teste-mp-api`; por isso esta entrega fica separada, para você aplicar na `main` sem mexer na branch atual.

## Opção A — aplicar o patch (recomendado, mantém autoria e mensagem)

```bash
cd ~/Documents/pagamento_redbull
git stash -u                      # se houver alterações locais na teste-mp-api
git fetch origin
git switch -c feat/controle-estoque origin/main
git am entrega_estoque/0001-controle-de-estoque.patch
pytest                            # esperado: 113 passed
git push -u origin feat/controle-estoque   # e abra o PR para a main
```

Se a `main` tiver avançado e o `git am` reclamar, use `git am -3 …` para resolver o conflito com merge de 3 vias.

## Opção B — copiar os arquivos

A pasta `arquivos/` tem os 23 arquivos novos ou alterados, com os mesmos caminhos do repositório. Copie por cima de um checkout da `main`.

## Depois de aplicar

```bash
alembic upgrade head                                   # 7e03d1bf5a42 -> c3f8a1d27b54
psql "postgresql://redbull:redbull@localhost:5432/redbull" -f sql/seed_sabores.sql   # opcional: 12 sabores
python -m scripts.create_staff_user --username felipe --name "Felipe Komata" --role ADMIN
STOCK_SESSION_COOKIE_SECURE=false uvicorn app.main:app --reload   # local, sem HTTPS
```

Acesse `http://127.0.0.1:8000/estoque`.

O plano completo (fluxo, modelo de dados, checklist de homologação/produção, riscos e rollback) está em `arquivos/docs/plano-acao-estoque.md`.

`capturas/` tem imagens do painel funcionando contra um PostgreSQL de teste.

Depois de aplicar, esta pasta `entrega_estoque/` pode ser apagada (não faça commit dela).
