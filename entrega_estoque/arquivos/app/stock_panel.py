"""Rotas do painel de controle de estoque (``/estoque`` e ``/api/estoque/*``).

As rotas são finas: autenticação fica em ``staff_auth`` e as regras de
estoque em ``stock_service``.
"""

from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession

from .db.base import get_db
from .models import (
    StaffLogin,
    StaffUser,
    StockMovement,
    StockMovementCreate,
    StockMovementResult,
    StockProduct,
    StockSummary,
)
from .staff_auth import (
    SESSION_COOKIE_NAME,
    StaffPrincipal,
    authenticate,
    clear_session_cookie,
    require_panel_header,
    require_staff,
    revoke_session,
    set_session_cookie,
)
from .stock_service import (
    apply_panel_movement,
    list_movements,
    list_stock_products,
    stock_summary,
)

PANEL_HTML = Path(__file__).resolve().parent / "templates" / "estoque.html"
NO_STORE = {"Cache-Control": "no-store"}
PANEL_PAGE_HEADERS = {
    **NO_STORE,
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
    "Content-Security-Policy": (
        "default-src 'self'; img-src 'self' data:; style-src 'self'; "
        "script-src 'self'; connect-src 'self'; frame-ancestors 'none'; "
        "base-uri 'none'; form-action 'self'"
    ),
}

router = APIRouter()
api = APIRouter(prefix="/api/estoque", tags=["estoque"])


@router.get("/estoque", include_in_schema=False)
def stock_panel_page() -> FileResponse:
    # O HTML fica fora de /static para só existir com estes cabeçalhos.
    return FileResponse(PANEL_HTML, headers=PANEL_PAGE_HEADERS)


@api.post(
    "/login",
    response_model=StaffUser,
    dependencies=[Depends(require_panel_header)],
)
async def login(
    payload: StaffLogin,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> StaffUser:
    staff, token = await authenticate(
        db,
        payload.username,
        payload.password,
        user_agent=request.headers.get("user-agent"),
        ip_address=request.client.host if request.client else None,
    )
    set_session_cookie(response, token)
    response.headers.update(NO_STORE)
    return staff


@api.post("/logout", status_code=204, dependencies=[Depends(require_panel_header)])
async def logout(request: Request, db: AsyncSession = Depends(get_db)) -> Response:
    await revoke_session(db, request.cookies.get(SESSION_COOKIE_NAME))
    response = Response(status_code=204)
    clear_session_cookie(response)
    return response


@api.get("/me", response_model=StaffUser)
async def me(
    response: Response,
    staff: StaffPrincipal = Depends(require_staff),
) -> StaffUser:
    response.headers.update(NO_STORE)
    return staff.to_model()


@api.get("/produtos", response_model=list[StockProduct])
async def stock_products(
    response: Response,
    _: StaffPrincipal = Depends(require_staff),
    db: AsyncSession = Depends(get_db),
) -> list[StockProduct]:
    response.headers.update(NO_STORE)
    return await list_stock_products(db)


@api.post(
    "/movimentos",
    response_model=StockMovementResult,
    status_code=201,
    dependencies=[Depends(require_panel_header)],
)
async def create_movement(
    payload: StockMovementCreate,
    staff: StaffPrincipal = Depends(require_staff),
    db: AsyncSession = Depends(get_db),
) -> StockMovementResult:
    return await apply_panel_movement(db, payload, staff.to_model())


@api.get("/movimentos", response_model=list[StockMovement])
async def movements(
    response: Response,
    product_id: str | None = Query(None, max_length=50),
    direction: Literal["in", "out"] | None = None,
    limit: int = Query(50, ge=1, le=200),
    before_id: int | None = Query(None, ge=1),
    _: StaffPrincipal = Depends(require_staff),
    db: AsyncSession = Depends(get_db),
) -> list[StockMovement]:
    response.headers.update(NO_STORE)
    return await list_movements(
        db,
        product_id=product_id,
        direction=direction,
        limit=limit,
        before_id=before_id,
    )


@api.get("/resumo", response_model=StockSummary)
async def summary(
    response: Response,
    _: StaffPrincipal = Depends(require_staff),
    db: AsyncSession = Depends(get_db),
) -> StockSummary:
    response.headers.update(NO_STORE)
    return await stock_summary(db)


router.include_router(api)
