"""Autenticação dos operadores do painel de estoque.

- Senhas: ``hashlib.scrypt`` (biblioteca padrão), salt aleatório por usuário.
- Sessões: token aleatório em cookie HttpOnly + SameSite=Strict; no banco fica
  apenas o SHA-256 do token (``staff_sessions.token_hash``).
- CSRF: rotas que alteram estado exigem o cabeçalho ``X-Requested-With``, que
  um site de terceiros não consegue enviar sem CORS.
- Força bruta: após ``STOCK_LOGIN_MAX_ATTEMPTS`` falhas, a conta fica
  bloqueada por ``STOCK_LOGIN_LOCK_MINUTES``; além disso, cada IP tem um
  limite de tentativas falhas (``STOCK_LOGIN_IP_MAX_FAILURES`` a cada 10 min).
- O scrypt roda numa thread (``asyncio.to_thread``) para não travar o event
  loop que também atende webhooks e a rotina de expiração.

Nunca registre senhas, tokens ou hashes em log.
"""

import asyncio
import base64
import hashlib
import hmac
import os
import secrets
import time
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from fastapi import Depends, Header, HTTPException, Request, Response
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from .db.base import get_db
from .db.models import StaffSessionORM, StaffUserORM
from .models import StaffRole, StaffUser

SESSION_COOKIE_NAME = "rb_estoque_session"
CSRF_HEADER_VALUE = "estoque-panel"
MIN_PASSWORD_LENGTH = 8

_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 32
# Hash de referência para gastar o mesmo tempo quando o usuário não existe.
_DUMMY_HASH: str | None = None


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} deve ser um número inteiro") from exc
    if value <= 0:
        raise RuntimeError(f"{name} deve ser maior que zero")
    return value


def session_ttl() -> timedelta:
    return timedelta(hours=_env_int("STOCK_SESSION_TTL_HOURS", 12))


def cookie_secure() -> bool:
    return os.getenv("STOCK_SESSION_COOKIE_SECURE", "true").strip().lower() not in {
        "0",
        "false",
        "no",
        "nao",
        "não",
    }


def max_login_attempts() -> int:
    return _env_int("STOCK_LOGIN_MAX_ATTEMPTS", 5)


def login_lock_duration() -> timedelta:
    return timedelta(minutes=_env_int("STOCK_LOGIN_LOCK_MINUTES", 15))


def ip_max_failures() -> int:
    return _env_int("STOCK_LOGIN_IP_MAX_FAILURES", 20)


IP_WINDOW_SECONDS = 600
_MAX_TRACKED_IPS = 5000


class LoginThrottle:
    """Limite de falhas de login por IP, em memória (uma instância por processo)."""

    def __init__(self) -> None:
        self._failures: dict[str, deque[float]] = {}

    def _recent(self, ip: str, now: float) -> deque[float]:
        attempts = self._failures.get(ip)
        if attempts is None:
            return deque()
        while attempts and now - attempts[0] > IP_WINDOW_SECONDS:
            attempts.popleft()
        if not attempts:
            self._failures.pop(ip, None)
        return attempts

    def is_blocked(self, ip: str) -> bool:
        return len(self._recent(ip, time.monotonic())) >= ip_max_failures()

    def record_failure(self, ip: str) -> None:
        now = time.monotonic()
        if len(self._failures) >= _MAX_TRACKED_IPS and ip not in self._failures:
            # Evita crescimento ilimitado: descarta o IP mais antigo.
            self._failures.pop(next(iter(self._failures)))
        attempts = self._failures.setdefault(ip, deque())
        attempts.append(now)

    def reset(self, ip: str) -> None:
        self._failures.pop(ip, None)

    def clear(self) -> None:
        self._failures.clear()


login_throttle = LoginThrottle()


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def hash_password(password: str) -> str:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"A senha precisa ter pelo menos {MIN_PASSWORD_LENGTH} caracteres")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_SCRYPT_DKLEN,
    )
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored_hash: str) -> bool:
    try:
        algorithm, n, r, p, salt_b64, digest_b64 = stored_hash.split("$")
        if algorithm != "scrypt":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(digest_b64)
        candidate = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(expected),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(candidate, expected)


def _dummy_hash() -> str:
    global _DUMMY_HASH
    if _DUMMY_HASH is None:
        _DUMMY_HASH = hash_password(secrets.token_urlsafe(16))
    return _DUMMY_HASH


def hash_session_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8", "surrogatepass")).hexdigest()


@dataclass(frozen=True)
class StaffPrincipal:
    id: int
    username: str
    full_name: str
    role: StaffRole
    session_id: int

    def to_model(self) -> StaffUser:
        return StaffUser(
            id=self.id,
            username=self.username,
            full_name=self.full_name,
            role=self.role,
        )


def require_panel_header(
    x_requested_with: str | None = Header(None),
) -> None:
    """Defesa simples contra CSRF para rotas que alteram estado."""
    if x_requested_with != CSRF_HEADER_VALUE:
        raise HTTPException(403, "Requisição recusada")


async def authenticate(
    db: AsyncSession,
    username: str,
    password: str,
    *,
    user_agent: str | None,
    ip_address: str | None,
) -> tuple[StaffUser, str]:
    """Valida usuário e senha e abre uma sessão. Retorna o token em claro.

    A verificação da senha acontece fora de qualquer transação/lock e numa
    thread separada; só depois a linha do usuário é travada (FOR NO KEY
    UPDATE, que não bloqueia inserts no livro-razão que referenciam o usuário).
    """
    username = username.strip().lower()
    ip_key = ip_address or "desconhecido"
    invalid = HTTPException(401, "Usuário ou senha inválidos")
    too_many = HTTPException(
        429,
        "Muitas tentativas de login. Aguarde alguns minutos e tente novamente.",
    )
    if login_throttle.is_blocked(ip_key):
        raise too_many

    async with db.begin():
        snapshot = (
            await db.execute(
                select(
                    StaffUserORM.id,
                    StaffUserORM.password_hash,
                    StaffUserORM.locked_until,
                ).where(StaffUserORM.username == username)
            )
        ).one_or_none()

    now = datetime.now(UTC)
    if snapshot is not None and snapshot.locked_until and snapshot.locked_until > now:
        login_throttle.record_failure(ip_key)
        raise too_many
    stored_hash = snapshot.password_hash if snapshot is not None else _dummy_hash()
    password_ok = await asyncio.to_thread(verify_password, password, stored_hash)
    if snapshot is None:
        login_throttle.record_failure(ip_key)
        raise invalid

    now = datetime.now(UTC)
    async with db.begin():
        user = (
            await db.execute(
                select(StaffUserORM)
                .where(StaffUserORM.id == snapshot.id)
                .with_for_update(key_share=True)
            )
        ).scalar_one_or_none()
        # A senha verificada precisa ser a mesma que está no banco agora.
        password_ok = (
            password_ok
            and user is not None
            and hmac.compare_digest(user.password_hash, snapshot.password_hash)
        )
        if user is None or not user.active or not password_ok:
            if user is not None and user.active and not password_ok:
                attempts = user.failed_login_attempts + 1
                user.failed_login_attempts = attempts
                if attempts >= max_login_attempts():
                    user.failed_login_attempts = 0
                    user.locked_until = now + login_lock_duration()
                user.updated_at = now
            # O commit acontece mesmo com a falha (contador de tentativas).
            failure = invalid
        else:
            failure = None
            user.failed_login_attempts = 0
            user.locked_until = None
            user.last_login_at = now
            user.updated_at = now
            token = secrets.token_urlsafe(32)
            db.add(
                StaffSessionORM(
                    staff_user_id=user.id,
                    token_hash=hash_session_token(token),
                    created_at=now,
                    expires_at=now + session_ttl(),
                    user_agent=(user_agent or "")[:255] or None,
                    ip_address=(ip_address or "")[:45] or None,
                )
            )
            staff = StaffUser(
                id=user.id,
                username=user.username,
                full_name=user.full_name,
                role=StaffRole(user.role),
            )

    if failure is not None:
        login_throttle.record_failure(ip_key)
        raise failure
    login_throttle.reset(ip_key)
    return staff, token


def set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        SESSION_COOKIE_NAME,
        token,
        max_age=int(session_ttl().total_seconds()),
        httponly=True,
        secure=cookie_secure(),
        samesite="strict",
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(
        SESSION_COOKIE_NAME,
        httponly=True,
        secure=cookie_secure(),
        samesite="strict",
        path="/",
    )


async def revoke_session(db: AsyncSession, token: str | None) -> None:
    if not token:
        return
    async with db.begin():
        await db.execute(
            update(StaffSessionORM)
            .where(
                StaffSessionORM.token_hash == hash_session_token(token),
                StaffSessionORM.revoked_at.is_(None),
            )
            .values(revoked_at=datetime.now(UTC))
        )


async def require_staff(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> StaffPrincipal:
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if not token:
        raise HTTPException(401, "Faça login para acessar o controle de estoque")
    now = datetime.now(UTC)
    async with db.begin():
        row = (
            await db.execute(
                select(StaffSessionORM.id, StaffUserORM)
                .join(StaffUserORM, StaffUserORM.id == StaffSessionORM.staff_user_id)
                .where(
                    StaffSessionORM.token_hash == hash_session_token(token),
                    StaffSessionORM.revoked_at.is_(None),
                    StaffSessionORM.expires_at > now,
                    StaffUserORM.active.is_(True),
                )
            )
        ).one_or_none()
    if row is None:
        raise HTTPException(401, "Sessão expirada. Faça login novamente.")
    session_id, user = row
    return StaffPrincipal(
        id=user.id,
        username=user.username,
        full_name=user.full_name,
        role=StaffRole(user.role),
        session_id=session_id,
    )
