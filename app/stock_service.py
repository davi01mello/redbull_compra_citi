"""Regras do controle de estoque (painel /estoque e registro de vendas).

O livro-razão ``stock_movements`` é escrito pelo trigger
``trg_products_stock_ledger`` sempre que ``products.stock`` muda. A aplicação
apenas informa *quem*, *por quê* e *para quem* por meio de
``set_stock_context``, chamado na mesma transação que altera o estoque.

Assim como em ``payment_service``, toda alteração de estoque segue o padrão:
``SELECT ... FOR UPDATE`` no produto dentro de ``db.begin()``, revalidação do
estado atual e só então a mutação.
"""

import json
import logging
import os
from datetime import datetime
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import and_, case, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from .db.models import ProductORM, StaffUserORM, StockMovementORM
from .models import (
    StaffRole,
    StaffUser,
    StockMovement,
    StockMovementCreate,
    StockMovementResult,
    StockMovementSource,
    StockMovementType,
    StockProduct,
    StockSummary,
)

logger = logging.getLogger(__name__)

STOCK_CONTEXT_SETTING = "redbull.stock_context"
MAX_PHYSICAL_STOCK = 10000
# "Saídas" e "entradas" dos totais do dia contam só movimentos reais de latas.
# Ajustes de inventário e alterações sem contexto são correções: aparecem no
# histórico, mas não inflam os totais.
OUT_TYPES = (
    StockMovementType.MARKETPLACE_SALE.value,
    StockMovementType.MANUAL_SALE.value,
    StockMovementType.LOSS.value,
)
IN_TYPES = (
    StockMovementType.RESTOCK.value,
    StockMovementType.INITIAL_STOCK.value,
)


def stock_timezone() -> str:
    return os.getenv("STOCK_TIMEZONE", "America/Recife").strip() or "America/Recife"


def _start_of_local_day():
    """Início do dia corrente no fuso do ponto de venda, calculado no banco."""
    tz = stock_timezone()
    return func.timezone(tz, func.date_trunc("day", func.timezone(tz, func.now())))


async def set_stock_context(
    db: AsyncSession,
    *,
    movement_type: StockMovementType,
    source: StockMovementSource,
    order_id: UUID | None = None,
    staff_user_id: int | None = None,
    buyer_name: str | None = None,
    buyer_email: str | None = None,
    note: str | None = None,
) -> None:
    """Informa ao trigger o contexto da próxima alteração de estoque.

    Usa ``set_config(..., true)``: o valor vale só até o fim da transação
    atual, então é seguro com pool de conexões. Precisa ser chamado dentro
    da mesma transação que altera ``products.stock``.
    """
    payload = {
        "movement_type": movement_type.value,
        "source": source.value,
        "order_id": str(order_id) if order_id else None,
        "staff_user_id": staff_user_id,
        "buyer_name": buyer_name,
        "buyer_email": buyer_email,
        "note": note,
    }
    await db.execute(
        text("SELECT set_config(:name, :value, true)"),
        {
            "name": STOCK_CONTEXT_SETTING,
            "value": json.dumps(
                {key: value for key, value in payload.items() if value is not None},
                ensure_ascii=False,
            ),
        },
    )


def _movement_to_model(
    movement: StockMovementORM,
    staff: StaffUserORM | None,
) -> StockMovement:
    return StockMovement(
        id=movement.id,
        product_id=movement.product_id,
        product_name=movement.product_name,
        movement_type=StockMovementType(movement.movement_type),
        source=StockMovementSource(movement.source),
        quantity=movement.quantity,
        delta=movement.delta,
        stock_before=movement.stock_before,
        stock_after=movement.stock_after,
        order_id=movement.order_id,
        buyer_name=movement.buyer_name,
        buyer_email=movement.buyer_email,
        unit_price=float(movement.unit_price) if movement.unit_price is not None else None,
        note=movement.note,
        staff_username=staff.username if staff else None,
        staff_name=staff.full_name if staff else None,
        created_at=movement.created_at,
    )


def _product_to_stock_model(
    product: ProductORM,
    out_today: int = 0,
    last_movement_at: datetime | None = None,
) -> StockProduct:
    return StockProduct(
        id=product.id,
        name=product.name,
        category=product.category,
        stock=product.stock,
        reserved_stock=product.reserved_stock,
        available=product.stock - product.reserved_stock,
        out_today=out_today,
        last_movement_at=last_movement_at,
    )


async def list_stock_products(db: AsyncSession) -> list[StockProduct]:
    out_today = (
        select(
            StockMovementORM.product_id.label("product_id"),
            func.sum(-StockMovementORM.delta).label("units"),
        )
        .where(
            StockMovementORM.movement_type.in_(OUT_TYPES),
            StockMovementORM.created_at >= _start_of_local_day(),
        )
        .group_by(StockMovementORM.product_id)
        .subquery()
    )
    last_movement = (
        select(
            StockMovementORM.product_id.label("product_id"),
            func.max(StockMovementORM.created_at).label("last_at"),
        )
        .group_by(StockMovementORM.product_id)
        .subquery()
    )
    rows = await db.execute(
        select(ProductORM, out_today.c.units, last_movement.c.last_at)
        .outerjoin(out_today, out_today.c.product_id == ProductORM.id)
        .outerjoin(last_movement, last_movement.c.product_id == ProductORM.id)
        .order_by(ProductORM.category, ProductORM.name)
    )
    return [
        _product_to_stock_model(product, int(units or 0), last_at)
        for product, units, last_at in rows
    ]


async def apply_panel_movement(
    db: AsyncSession,
    payload: StockMovementCreate,
    staff: StaffUser,
) -> StockMovementResult:
    """Lança uma entrada/saída manual de forma atômica e auditada."""
    movement_type = payload.movement_type
    if movement_type == StockMovementType.ADJUSTMENT and staff.role != StaffRole.ADMIN:
        raise HTTPException(403, "Somente administradores podem ajustar o inventário")

    async with db.begin():
        product = (
            await db.execute(
                select(ProductORM)
                .where(ProductORM.id == payload.product_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if product is None:
            raise HTTPException(404, "Sabor não encontrado")

        if movement_type == StockMovementType.ADJUSTMENT:
            new_stock = payload.counted_stock
        elif movement_type == StockMovementType.RESTOCK:
            new_stock = product.stock + payload.quantity
        else:
            new_stock = product.stock - payload.quantity

        if new_stock == product.stock:
            raise HTTPException(409, "A contagem informada é igual ao estoque atual")
        if new_stock > MAX_PHYSICAL_STOCK:
            raise HTTPException(
                422, f"O estoque não pode passar de {MAX_PHYSICAL_STOCK} unidades"
            )
        if new_stock < 0 and product.reserved_stock == 0:
            raise HTTPException(
                409,
                f"Estoque insuficiente: há apenas {product.stock} lata(s) de {product.name}.",
            )
        if new_stock < product.reserved_stock:
            available = max(product.stock - product.reserved_stock, 0)
            raise HTTPException(
                409,
                f"Só há {available} lata(s) de {product.name} livre(s) para saída: "
                f"{product.reserved_stock} estão reservadas para pagamentos PIX em andamento.",
            )

        previous_stock = product.stock
        await set_stock_context(
            db,
            movement_type=movement_type,
            source=StockMovementSource.PAINEL,
            staff_user_id=staff.id,
            buyer_name=payload.buyer_name,
            buyer_email=payload.buyer_email,
            note=payload.note,
        )
        product.stock = new_stock
        await db.flush()

        # O produto está travado nesta transação: a última linha do
        # livro-razão para ele deve ser a que o trigger acabou de inserir.
        # Se não for (trigger ausente/desativado ou contexto descartado), a
        # transação inteira é desfeita: estoque nunca muda sem registro.
        movement = (
            await db.execute(
                select(StockMovementORM)
                .where(StockMovementORM.product_id == product.id)
                .order_by(StockMovementORM.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if (
            movement is None
            or movement.movement_type != movement_type.value
            or movement.staff_user_id != staff.id
            or movement.stock_before != previous_stock
            or movement.stock_after != new_stock
        ):
            logger.error(
                "Livro-razão de estoque não registrou a movimentação do painel: "
                "product_id=%s movement_type=%s",
                product.id,
                movement_type.value,
            )
            raise HTTPException(
                500,
                "Não foi possível registrar a movimentação no histórico. Nada foi alterado.",
            )
        out_today = (
            await db.execute(
                select(func.coalesce(func.sum(-StockMovementORM.delta), 0)).where(
                    StockMovementORM.product_id == product.id,
                    StockMovementORM.movement_type.in_(OUT_TYPES),
                    StockMovementORM.created_at >= _start_of_local_day(),
                )
            )
        ).scalar_one()
        staff_row = await db.get(StaffUserORM, staff.id)
        result = StockMovementResult(
            movement=_movement_to_model(movement, staff_row),
            product=_product_to_stock_model(
                product,
                out_today=int(out_today),
                last_movement_at=movement.created_at,
            ),
        )

    return result


async def list_movements(
    db: AsyncSession,
    *,
    product_id: str | None = None,
    direction: str | None = None,
    limit: int = 50,
    before_id: int | None = None,
) -> list[StockMovement]:
    query = (
        select(StockMovementORM, StaffUserORM)
        .outerjoin(StaffUserORM, StaffUserORM.id == StockMovementORM.staff_user_id)
        .order_by(StockMovementORM.id.desc())
        .limit(limit)
    )
    if product_id:
        query = query.where(StockMovementORM.product_id == product_id)
    if direction == "out":
        query = query.where(StockMovementORM.delta < 0)
    elif direction == "in":
        query = query.where(StockMovementORM.delta > 0)
    if before_id:
        query = query.where(StockMovementORM.id < before_id)
    rows = await db.execute(query)
    return [_movement_to_model(movement, staff) for movement, staff in rows]


async def stock_summary(db: AsyncSession) -> StockSummary:
    today = _start_of_local_day()
    is_today = StockMovementORM.created_at >= today

    def units_where(*conditions):
        return func.coalesce(
            func.sum(case((and_(is_today, *conditions), StockMovementORM.quantity), else_=0)),
            0,
        )

    movement_totals = (
        await db.execute(
            select(
                units_where(StockMovementORM.movement_type.in_(OUT_TYPES)),
                units_where(StockMovementORM.movement_type.in_(IN_TYPES)),
                units_where(
                    StockMovementORM.movement_type == StockMovementType.MARKETPLACE_SALE.value
                ),
                units_where(
                    StockMovementORM.movement_type == StockMovementType.MANUAL_SALE.value
                ),
                units_where(StockMovementORM.movement_type == StockMovementType.LOSS.value),
            ).where(is_today)
        )
    ).one()
    stock_totals = (
        await db.execute(
            select(
                func.coalesce(func.sum(ProductORM.stock), 0),
                func.coalesce(func.sum(ProductORM.reserved_stock), 0),
            )
        )
    ).one()
    local_day = (
        await db.execute(select(func.to_char(func.timezone(stock_timezone(), func.now()), "YYYY-MM-DD")))
    ).scalar_one()
    total_stock, total_reserved = (int(value) for value in stock_totals)
    out_today, in_today, marketplace, manual, losses = (int(value) for value in movement_totals)
    return StockSummary(
        day=local_day,
        units_out_today=out_today,
        units_in_today=in_today,
        marketplace_sales_today=marketplace,
        manual_sales_today=manual,
        losses_today=losses,
        total_stock=total_stock,
        total_reserved=total_reserved,
        total_available=total_stock - total_reserved,
    )
