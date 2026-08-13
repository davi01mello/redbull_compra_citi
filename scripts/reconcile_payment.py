import argparse
import asyncio
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select

from app.config import settings
from app.db.base import AsyncSessionLocal, engine
from app.db.models import OrderORM, PaymentORM
from app.models import PaymentProvider as PaymentProviderName
from app.payment_service import (
    process_mercadopago_payment,
    reconciliation_event_id,
)
from app.payments import PaymentProviderError, create_payment_provider


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Reconcilia com segurança um pagamento Mercado Pago.",
    )
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--order-id", type=UUID, help="ID interno do pedido")
    target.add_argument(
        "--provider-order-id",
        help="ID da Order no Mercado Pago",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Consulta e descreve as alterações sem modificar o banco",
    )
    return parser.parse_args()


async def run(args: argparse.Namespace) -> int:
    if settings.payment_provider != "mercadopago":
        raise RuntimeError(
            "Configure PAYMENT_PROVIDER=mercadopago para executar a reconciliação"
        )
    provider = create_payment_provider(settings)
    try:
        async with AsyncSessionLocal() as db:
            statement = (
                select(PaymentORM, OrderORM)
                .join(OrderORM, OrderORM.id == PaymentORM.order_id)
                .where(PaymentORM.provider == PaymentProviderName.MERCADOPAGO)
            )
            if args.order_id:
                statement = statement.where(PaymentORM.order_id == args.order_id)
            else:
                statement = statement.where(
                    PaymentORM.provider_order_id == args.provider_order_id
                )
            if not args.dry_run:
                statement = statement.with_for_update()
            row = (await db.execute(statement)).one_or_none()
            if row is None:
                raise RuntimeError("Pagamento Mercado Pago não encontrado")

            payment, order = row
            provider_order_id = payment.provider_order_id
            if not provider_order_id:
                raise RuntimeError("Pagamento não possui provider_order_id")

            try:
                remote = await provider.get_order(provider_order_id)
            except PaymentProviderError as exc:
                raise RuntimeError(
                    "Não foi possível consultar a Order no Mercado Pago"
                ) from exc

            if remote.provider_order_id != provider_order_id:
                raise RuntimeError(
                    "A resposta remota não corresponde ao provider_order_id local"
                )
            amount_matches = Decimal(payment.amount) == remote.amount

            print(f"order_id={order.id}")
            print(f"provider_order_id={provider_order_id}")
            print(f"local_order_status={order.status.value}")
            print(f"local_payment_status={payment.status.value}")
            print(f"local_reservation_status={order.reservation_status.value}")
            print(f"remote_status={remote.status}")
            print(f"amount_matches={str(amount_matches).lower()}")

            if not amount_matches:
                raise RuntimeError(
                    "O valor remoto diverge do pagamento local; nenhuma alteração foi feita"
                )

            if args.dry_run:
                if remote.status.upper() == "APPROVED":
                    print(
                        "would_change=pedido/pagamento/reserva/estoque "
                        "pela transação central, se ainda pendentes"
                    )
                elif remote.status.upper() in {"CANCELLED", "EXPIRED"}:
                    print(
                        "would_change=status terminal e liberação segura da reserva, "
                        "se ainda pendentes"
                    )
                else:
                    print("would_change=none")
                print("dry_run=true")
                return 0

            await db.rollback()
            result = await process_mercadopago_payment(
                db,
                event_id=reconciliation_event_id(
                    "manual-reconciliation", provider_order_id
                ),
                event_type="manual.reconciliation",
                webhook_order_id=provider_order_id,
                provider_payment=remote,
            )
            print(f"outcome={result.outcome}")
            print(f"processed={str(result.processed).lower()}")
            return 0
    finally:
        await provider.aclose()
        await engine.dispose()


def main() -> None:
    try:
        exit_code = asyncio.run(run(parse_args()))
    except RuntimeError as exc:
        raise SystemExit(f"Erro: {exc}") from exc
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
