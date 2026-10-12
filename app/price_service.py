"""Tabela de preços editada pelo painel ``/estoque``.

O administrador informa só o preço base ``x`` (preço unitário de 1 lata).

Membros têm a promoção por quantidade total do pedido, derivada de ``x``:

- 1 lata: ``x``
- 2 ou 3 latas: ``x - 0,50`` por lata
- 4 latas ou mais: ``x - 1,00`` por lata

Não membros não recebem promoção: uma única faixa (1 lata ou mais) a ``x``.

As faixas antigas não são apagadas nem editadas: são desativadas e novas
linhas são inseridas na mesma transação. Pedidos já criados continuam
apontando para a faixa (e o ``unit_price`` em ``order_items``) da época.
"""

import logging
from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from .db.models import PriceTierORM
from .models import (
    PriceTable,
    PriceTableUpdate,
    PriceTier,
    PricingAudience,
    StaffRole,
    StaffUser,
)

logger = logging.getLogger(__name__)

CENTS = Decimal("0.01")
# (quantidade mínima, quantidade máxima, desconto por lata sobre o preço base)
PRICE_TIER_DISCOUNTS: tuple[tuple[int, int | None, Decimal], ...] = (
    (1, 1, Decimal("0.00")),
    (2, 3, Decimal("0.50")),
    (4, None, Decimal("1.00")),
)


def tiers_for_base_price(
    base_price: Decimal,
    audience: PricingAudience = PricingAudience.MEMBER,
) -> list[tuple[int, int | None, Decimal]]:
    if audience != PricingAudience.MEMBER:
        return [(1, None, base_price.quantize(CENTS))]
    return [
        (min_quantity, max_quantity, (base_price - discount).quantize(CENTS))
        for min_quantity, max_quantity, discount in PRICE_TIER_DISCOUNTS
    ]


def _tier_to_model(tier: PriceTierORM) -> PriceTier:
    return PriceTier(
        id=tier.id,
        pricing_audience=tier.pricing_audience,
        min_quantity=tier.min_quantity,
        max_quantity=tier.max_quantity,
        unit_price=float(tier.unit_price),
    )


def _price_table(audience: PricingAudience, tiers: list[PriceTierORM]) -> PriceTable:
    base = next((tier for tier in tiers if tier.min_quantity == 1), None)
    return PriceTable(
        pricing_audience=audience,
        base_price=float(base.unit_price) if base else None,
        tiers=[_tier_to_model(tier) for tier in tiers],
    )


async def _active_tiers(db: AsyncSession, audience: PricingAudience) -> list[PriceTierORM]:
    result = await db.execute(
        select(PriceTierORM)
        .where(
            PriceTierORM.pricing_audience == audience,
            PriceTierORM.active.is_(True),
        )
        .order_by(PriceTierORM.min_quantity)
        .execution_options(populate_existing=True)
    )
    return list(result.scalars())


async def list_price_tables(db: AsyncSession) -> list[PriceTable]:
    return [
        _price_table(audience, await _active_tiers(db, audience))
        for audience in PricingAudience
    ]


async def update_price_table(
    db: AsyncSession,
    payload: PriceTableUpdate,
    staff: StaffUser,
) -> PriceTable:
    """Troca as faixas ativas de um público pelas derivadas do novo preço base."""
    if staff.role != StaffRole.ADMIN:
        raise HTTPException(403, "Somente administradores podem alterar os preços")

    audience = payload.pricing_audience
    if audience == PricingAudience.MEMBER and payload.base_price <= PRICE_TIER_DISCOUNTS[-1][2]:
        raise HTTPException(
            422, "O preço de membros precisa ser maior que R$ 1,00 (desconto da última faixa)"
        )
    new_tiers = tiers_for_base_price(payload.base_price, audience)

    async with db.begin():
        # Serializa alterações concorrentes do mesmo público: sem isso, duas
        # trocas simultâneas poderiam deixar dois conjuntos de faixas ativos.
        await db.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
            {"key": f"price_tiers:{audience.value}"},
        )
        current = await _active_tiers(db, audience)
        if [
            (tier.min_quantity, tier.max_quantity, Decimal(tier.unit_price))
            for tier in current
        ] == new_tiers:
            raise HTTPException(409, "Esses já são os preços atuais")

        await db.execute(
            update(PriceTierORM)
            .where(
                PriceTierORM.pricing_audience == audience,
                PriceTierORM.active.is_(True),
            )
            .values(active=False)
        )
        created = [
            PriceTierORM(
                pricing_audience=audience,
                min_quantity=min_quantity,
                max_quantity=max_quantity,
                unit_price=unit_price,
                active=True,
            )
            for min_quantity, max_quantity, unit_price in new_tiers
        ]
        db.add_all(created)
        await db.flush()
        table = _price_table(audience, created)

    logger.info(
        "Preços atualizados pelo painel: audience=%s base_price=%s staff_id=%s",
        audience.value,
        payload.base_price,
        staff.id,
    )
    return table
