"""Cria ou atualiza operadores do painel de estoque.

Exemplos:

    # cria um administrador (a senha é pedida no terminal, sem eco)
    python -m scripts.create_staff_user --username felipe --name "Felipe Komata" --role ADMIN

    # cria um operador
    python -m scripts.create_staff_user --username caixa01 --name "Caixa 01"

    # redefine a senha e desbloqueia
    python -m scripts.create_staff_user --username caixa01 --reset-password

    # desativa (revoga as sessões abertas)
    python -m scripts.create_staff_user --username caixa01 --deactivate

Para automação, a senha pode vir pela entrada padrão com --password-stdin.
A senha nunca é aceita como argumento de linha de comando (ficaria no histórico).
"""

import argparse
import asyncio
import getpass
import sys
from datetime import UTC, datetime

from sqlalchemy import select, update

from app.db.base import AsyncSessionLocal, engine
from app.db.models import StaffSessionORM, StaffUserORM
from app.models import StaffRole, normalize_username
from app.staff_auth import MIN_PASSWORD_LENGTH, hash_password


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Cria ou atualiza operadores do painel de estoque (/estoque).",
    )
    parser.add_argument("--username", required=True, help="login (minúsculas, 3–50)")
    parser.add_argument("--name", help="nome completo exibido no painel")
    parser.add_argument(
        "--role",
        choices=[role.value for role in StaffRole],
        help="ADMIN pode ajustar inventário; OPERADOR lança entradas e saídas",
    )
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--reset-password", action="store_true")
    action.add_argument("--deactivate", action="store_true")
    action.add_argument("--activate", action="store_true")
    parser.add_argument(
        "--password-stdin",
        action="store_true",
        help="lê a senha da primeira linha da entrada padrão",
    )
    return parser.parse_args()


def read_password(from_stdin: bool) -> str:
    if from_stdin:
        password = sys.stdin.readline().rstrip("\n")
    else:
        password = getpass.getpass("Senha: ")
        if getpass.getpass("Repita a senha: ") != password:
            raise SystemExit("As senhas não conferem.")
    if len(password) < MIN_PASSWORD_LENGTH:
        raise SystemExit(f"A senha precisa ter pelo menos {MIN_PASSWORD_LENGTH} caracteres.")
    return password


async def run(args: argparse.Namespace) -> int:
    try:
        username = normalize_username(args.username)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    if (args.deactivate or args.activate) and args.password_stdin:
        raise SystemExit("--password-stdin só faz sentido ao criar ou redefinir senha.")

    async with AsyncSessionLocal() as db:
        exists = (
            await db.execute(
                select(StaffUserORM.id).where(StaffUserORM.username == username)
            )
        ).scalar_one_or_none() is not None

    if not exists and (args.reset_password or args.deactivate or args.activate):
        raise SystemExit(f"Usuário '{username}' não existe.")

    # A senha é lida antes de abrir a transação: nenhum lock fica preso
    # enquanto o terminal espera a digitação.
    password_hash = None
    if args.reset_password or (not exists and not (args.deactivate or args.activate)):
        if not exists and (not args.name or len(args.name.strip()) < 2):
            raise SystemExit("Informe --name para criar o usuário.")
        password_hash = hash_password(read_password(args.password_stdin))

    now = datetime.now(UTC)
    async with AsyncSessionLocal() as db, db.begin():
        user = (
            await db.execute(
                select(StaffUserORM)
                .where(StaffUserORM.username == username)
                .with_for_update(key_share=True)
            )
        ).scalar_one_or_none()

        if user is None:
            if args.reset_password or args.deactivate or args.activate:
                raise SystemExit(f"Usuário '{username}' não existe.")
            if password_hash is None:
                raise SystemExit(f"Usuário '{username}' foi removido; rode o comando de novo.")
            db.add(
                StaffUserORM(
                    username=username,
                    full_name=args.name.strip()[:120],
                    password_hash=password_hash,
                    role=args.role or StaffRole.OPERADOR.value,
                    active=True,
                )
            )
            print(f"Usuário '{username}' criado ({args.role or StaffRole.OPERADOR.value}).")
            return 0

        if args.reset_password:
            user.password_hash = password_hash
            user.failed_login_attempts = 0
            user.locked_until = None
        if args.deactivate:
            user.active = False
        if args.activate:
            user.active = True
            user.failed_login_attempts = 0
            user.locked_until = None
        if args.name:
            user.full_name = args.name.strip()[:120]
        if args.role:
            user.role = args.role
        user.updated_at = now

        if args.reset_password or args.deactivate:
            await db.execute(
                update(StaffSessionORM)
                .where(
                    StaffSessionORM.staff_user_id == user.id,
                    StaffSessionORM.revoked_at.is_(None),
                )
                .values(revoked_at=now)
            )
        print(f"Usuário '{username}' atualizado.")
    return 0


async def run_and_dispose(args: argparse.Namespace) -> int:
    try:
        return await run(args)
    finally:
        await engine.dispose()


def main() -> None:
    raise SystemExit(asyncio.run(run_and_dispose(parse_args())))


if __name__ == "__main__":
    main()
