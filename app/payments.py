from __future__ import annotations

import hashlib
import hmac
import logging
import re
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Protocol

import httpx

from .config import Settings


MERCADOPAGO_API_URL = "https://api.mercadopago.com"
logger = logging.getLogger(__name__)
EMAIL_IN_TEXT_PATTERN = re.compile(r"[^\s@]+@[^\s@]+")
BEARER_IN_TEXT_PATTERN = re.compile(r"(?i)bearer\s+[^\s,;]+")


def _sanitize_log_value(value: object, limit: int = 240) -> str:
    text = " ".join(str(value).split())
    text = EMAIL_IN_TEXT_PATTERN.sub("<redacted-email>", text)
    text = BEARER_IN_TEXT_PATTERN.sub("Bearer <redacted>", text)
    return text[:limit]


class PaymentProviderError(RuntimeError):
    pass


@dataclass(frozen=True)
class ChargeResult:
    provider_order_id: str
    provider_payment_id: str | None
    qr_code: str
    copy_paste: str
    amount: Decimal
    expires_at: datetime


@dataclass(frozen=True)
class PaymentResult:
    provider_order_id: str
    provider_payment_id: str | None
    status: str
    amount: Decimal
    currency: str
    approved_at: datetime | None


class PaymentProvider(Protocol):
    provider_name: str

    async def create_charge(
        self,
        external_reference: str,
        amount: Decimal,
        idempotency_key: str,
        payer_email: str,
    ) -> ChargeResult: ...

    async def get_order(self, order_id: str) -> PaymentResult: ...

    async def cancel_order(
        self,
        order_id: str,
        idempotency_key: str,
    ) -> None: ...

    async def aclose(self) -> None: ...


def _parse_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


class MockPaymentProvider:
    provider_name = "mock"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._orders: dict[str, PaymentResult] = {}

    async def create_charge(
        self,
        external_reference: str,
        amount: Decimal,
        idempotency_key: str,
        payer_email: str,
    ) -> ChargeResult:
        if not idempotency_key:
            raise PaymentProviderError("A chave de idempotência é obrigatória")
        provider_order_id = f"MOCK-ORDER-{external_reference}"
        provider_payment_id = f"MOCK-PAYMENT-{external_reference}"
        expires_at = datetime.now(UTC) + timedelta(
            seconds=self.settings.payment_checkout_expires_in
        )
        self._orders[provider_order_id] = PaymentResult(
            provider_order_id=provider_order_id,
            provider_payment_id=provider_payment_id,
            status="WAITING_PAYMENT",
            amount=amount,
            currency="BRL",
            approved_at=None,
        )
        return ChargeResult(
            provider_order_id=provider_order_id,
            provider_payment_id=provider_payment_id,
            qr_code=f"/api/mock/qr/{provider_payment_id}",
            copy_paste=(
                f"00020126PIX-SIMULADO-{external_reference}-VALOR-{amount:.2f}"
            ),
            amount=amount,
            expires_at=expires_at,
        )

    async def get_order(self, order_id: str) -> PaymentResult:
        try:
            return self._orders[order_id]
        except KeyError as exc:
            raise PaymentProviderError("Order mock não encontrada") from exc

    async def cancel_order(self, order_id: str, idempotency_key: str) -> None:
        if not idempotency_key:
            raise PaymentProviderError("A chave de idempotência é obrigatória")
        current = self._orders.get(order_id)
        if current is None:
            raise PaymentProviderError("Order mock não encontrada")
        self._orders[order_id] = replace(current, status="CANCELLED")

    async def aclose(self) -> None:
        return None


class MercadoPagoProvider:
    provider_name = "mercadopago"

    def __init__(
        self,
        settings: Settings,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.settings = settings
        self._headers = {
            "Authorization": f"Bearer {settings.mercadopago_access_token}",
            "Content-Type": "application/json",
            "User-Agent": "CITi-RedBull-PDV/2.0",
        }
        self._client = client or httpx.AsyncClient(
            base_url=MERCADOPAGO_API_URL,
            timeout=httpx.Timeout(15.0),
        )

    async def create_charge(
        self,
        external_reference: str,
        amount: Decimal,
        idempotency_key: str,
        payer_email: str,
    ) -> ChargeResult:
        if not idempotency_key:
            raise PaymentProviderError("A chave de idempotência é obrigatória")

        amount_text = f"{amount:.2f}"
        now = datetime.now(UTC)
        provider_expires_at = now + timedelta(
            seconds=self.settings.charge_expires_in
        )
        checkout_expires_at = now + timedelta(
            seconds=self.settings.payment_checkout_expires_in
        )
        if not payer_email:
            raise PaymentProviderError("O pedido não possui e-mail do pagador")

        payload = {
            "type": "online",
            "total_amount": amount_text,
            "external_reference": external_reference,
            "processing_mode": "automatic",
            "transactions": {
                "payments": [
                    {
                        "amount": amount_text,
                        "payment_method": {
                            "id": "pix",
                            "type": "bank_transfer",
                        },
                        "expiration_time": f"PT{self.settings.charge_expires_in}S",
                    }
                ]
            },
            "payer": {"email": payer_email},
        }
        try:
            response = await self._client.post(
                "/v1/orders",
                headers={
                    **self._headers,
                    "X-Idempotency-Key": idempotency_key,
                },
                json=payload,
            )
            response.raise_for_status()
            data = response.json()
        except httpx.TimeoutException as exc:
            logger.warning(
                "Timeout Mercado Pago: operation=create_order order_id=%s",
                external_reference,
            )
            raise PaymentProviderError(
                "Tempo esgotado ao criar a cobrança no Mercado Pago"
            ) from exc
        except httpx.HTTPStatusError as exc:
            self._log_http_error("create_order", exc.response, external_reference)
            detail = self._error_detail(exc.response)
            raise PaymentProviderError(
                f"Mercado Pago recusou a cobrança: {detail}"
            ) from exc
        except httpx.TransportError as exc:
            logger.warning(
                "Falha de comunicação Mercado Pago: operation=create_order "
                "order_id=%s error=%s",
                external_reference,
                type(exc).__name__,
            )
            raise PaymentProviderError(
                "Falha de comunicação ao criar a cobrança no Mercado Pago"
            ) from exc
        except ValueError as exc:
            raise PaymentProviderError(
                "Resposta inválida ao criar a cobrança no Mercado Pago"
            ) from exc

        charge = self._parse_charge(data, amount, provider_expires_at)
        return replace(
            charge,
            expires_at=min(charge.expires_at, checkout_expires_at),
        )

    async def get_order(self, order_id: str) -> PaymentResult:
        try:
            response = await self._client.get(
                f"/v1/orders/{order_id}",
                headers=self._headers,
            )
            response.raise_for_status()
            data = response.json()
        except httpx.TimeoutException as exc:
            logger.warning(
                "Timeout Mercado Pago: operation=get_order "
                "provider_order_id=%s",
                order_id,
            )
            raise PaymentProviderError(
                "Tempo esgotado ao consultar a order no Mercado Pago"
            ) from exc
        except httpx.HTTPStatusError as exc:
            self._log_http_error("get_order", exc.response, order_id)
            detail = self._error_detail(exc.response)
            raise PaymentProviderError(
                f"Mercado Pago recusou a consulta: {detail}"
            ) from exc
        except httpx.TransportError as exc:
            logger.warning(
                "Falha de comunicação Mercado Pago: operation=get_order "
                "provider_order_id=%s error=%s",
                order_id,
                type(exc).__name__,
            )
            raise PaymentProviderError(
                "Falha de comunicação ao consultar a order no Mercado Pago"
            ) from exc
        except ValueError as exc:
            raise PaymentProviderError(
                "Resposta inválida ao consultar a order no Mercado Pago"
            ) from exc

        if not isinstance(data, dict):
            raise PaymentProviderError("O Mercado Pago retornou uma resposta inválida")
        provider_order_id = data.get("id")
        transactions = data.get("transactions")
        if not isinstance(transactions, dict):
            raise PaymentProviderError(
                "O Mercado Pago retornou uma order incompleta"
            )
        payments = transactions.get("payments")
        payment = payments[0] if isinstance(payments, list) and payments else None
        if not provider_order_id or not data.get("status") or not isinstance(payment, dict):
            raise PaymentProviderError("O Mercado Pago retornou uma order incompleta")

        order_status = str(data["status"]).lower()
        order_status_detail = str(data.get("status_detail") or "").lower()
        payment_status = str(payment.get("status") or "").lower()
        payment_status_detail = str(payment.get("status_detail") or "").lower()
        approved = (
            order_status == "processed"
            and order_status_detail == "accredited"
            and payment_status == "processed"
            and payment_status_detail == "accredited"
        )
        try:
            raw_amount = (
                payment.get("paid_amount")
                if approved
                else payment.get("amount")
            )
            if raw_amount is None:
                raw_amount = data.get("total_paid_amount") or data["total_amount"]
            amount = Decimal(str(raw_amount))
        except (KeyError, InvalidOperation, TypeError, ValueError) as exc:
            raise PaymentProviderError(
                "O Mercado Pago retornou um valor de pagamento inválido"
            ) from exc
        remote_status = self._map_order_status(
            order_status,
            order_status_detail,
            payment_status,
            payment_status_detail,
        )
        return PaymentResult(
            provider_order_id=str(provider_order_id),
            provider_payment_id=(
                str(payment["id"]) if payment.get("id") is not None else None
            ),
            status=remote_status,
            amount=amount,
            currency=str(data.get("currency") or "BRL"),
            approved_at=(
                (
                    _parse_datetime(payment.get("date_of_approval"))
                    or _parse_datetime(payment.get("date_approved"))
                    or _parse_datetime(data.get("last_updated_date"))
                )
                if approved
                else None
            ),
        )

    async def cancel_order(self, order_id: str, idempotency_key: str) -> None:
        if not idempotency_key:
            raise PaymentProviderError("A chave de idempotência é obrigatória")

        try:
            response = await self._client.post(
                f"/v1/orders/{order_id}/cancel",
                headers={
                    **self._headers,
                    "X-Idempotency-Key": idempotency_key,
                },
            )
            error_code = self._error_code(response)
            if response.status_code == 409 and error_code in {
                "order_already_canceled",
                "order_already_cancelled",
            }:
                return
            response.raise_for_status()
        except httpx.TimeoutException as exc:
            logger.warning(
                "Timeout Mercado Pago: operation=cancel_order "
                "provider_order_id=%s",
                order_id,
            )
            raise PaymentProviderError(
                "Tempo esgotado ao cancelar a order no Mercado Pago"
            ) from exc
        except httpx.HTTPStatusError as exc:
            self._log_http_error("cancel_order", exc.response, order_id)
            detail = self._error_detail(exc.response)
            raise PaymentProviderError(
                f"Mercado Pago recusou o cancelamento: {detail}"
            ) from exc
        except httpx.TransportError as exc:
            logger.warning(
                "Falha de comunicação Mercado Pago: operation=cancel_order "
                "provider_order_id=%s error=%s",
                order_id,
                type(exc).__name__,
            )
            raise PaymentProviderError(
                "Falha de comunicação ao cancelar a order no Mercado Pago"
            ) from exc

    async def aclose(self) -> None:
        await self._client.aclose()

    @staticmethod
    def _map_order_status(
        order_status: str,
        order_status_detail: str,
        payment_status: str,
        payment_status_detail: str,
    ) -> str:
        if (
            order_status == "processed"
            and order_status_detail == "accredited"
            and payment_status == "processed"
            and payment_status_detail == "accredited"
        ):
            return "APPROVED"
        combined = {
            order_status,
            order_status_detail,
            payment_status,
            payment_status_detail,
        }
        if combined & {"cancelled", "canceled"}:
            return "CANCELLED"
        if "expired" in combined:
            return "EXPIRED"
        return "WAITING_PAYMENT"

    @staticmethod
    def _parse_charge(
        data: object,
        amount: Decimal,
        fallback_expires_at: datetime,
    ) -> ChargeResult:
        if not isinstance(data, dict):
            raise PaymentProviderError("O Mercado Pago retornou uma resposta inválida")
        provider_order_id = data.get("id")
        transactions = data.get("transactions")
        if not isinstance(transactions, dict):
            raise PaymentProviderError("O Mercado Pago retornou uma resposta inválida")
        payments = transactions.get("payments", [])
        payment = payments[0] if isinstance(payments, list) and payments else {}
        if not isinstance(payment, dict):
            raise PaymentProviderError("O Mercado Pago retornou uma resposta inválida")
        payment_method = payment.get("payment_method", {})
        if not isinstance(payment_method, dict):
            raise PaymentProviderError("O Mercado Pago retornou uma resposta inválida")
        copy_paste = payment_method.get("qr_code")
        qr_code = payment_method.get("ticket_url")
        if not provider_order_id:
            raise PaymentProviderError(
                "O Mercado Pago não retornou o identificador da cobrança"
            )
        if not copy_paste:
            raise PaymentProviderError("O Mercado Pago não retornou o código PIX")
        if not qr_code:
            raise PaymentProviderError(
                "O Mercado Pago não retornou a URL do QR Code PIX"
            )
        return ChargeResult(
            provider_order_id=str(provider_order_id),
            provider_payment_id=(
                str(payment["id"]) if payment.get("id") is not None else None
            ),
            qr_code=str(qr_code or ""),
            copy_paste=str(copy_paste),
            amount=amount,
            expires_at=(
                _parse_datetime(payment.get("date_of_expiration"))
                or fallback_expires_at
            ),
        )

    @staticmethod
    def _error_code(response: httpx.Response) -> str | None:
        try:
            data = response.json()
        except ValueError:
            return None

        def find_code(value: object, field: str | None = None) -> str | None:
            if isinstance(value, dict):
                for key in ("code", "error"):
                    candidate = value.get(key)
                    if isinstance(candidate, (str, int)) and candidate:
                        return str(candidate)
                for key in ("errors", "details", "cause"):
                    if key in value:
                        candidate = find_code(value[key], key)
                        if candidate:
                            return candidate
                return None
            if isinstance(value, list):
                for item in value:
                    candidate = find_code(item, field)
                    if candidate:
                        return candidate
            if field in {"code", "error"} and isinstance(value, (str, int)):
                return str(value)
            return None

        return find_code(data)

    @staticmethod
    def _error_detail(response: httpx.Response) -> str:
        parts = [f"status={response.status_code}"]

        try:
            data = response.json()
        except ValueError:
            return " ".join(parts)

        if not isinstance(data, dict):
            return " ".join(parts)

        for key in ("code", "error", "status"):
            value = data.get(key)
            if isinstance(value, (str, int)) and value:
                parts.append(f"{key}={_sanitize_log_value(value, 120)}")
        message = data.get("message")
        if isinstance(message, str) and message:
            sanitized = _sanitize_log_value(message)
            parts.append(f"message={sanitized}")
        nested_code = MercadoPagoProvider._error_code(response)
        if nested_code and f"code={nested_code}" not in parts:
            parts.append(f"code={nested_code}")

        return " ".join(parts)

    @staticmethod
    def _log_http_error(
        operation: str,
        response: httpx.Response,
        reference: str,
    ) -> None:
        logger.warning(
            "Erro HTTP Mercado Pago: operation=%s reference=%s status=%s "
            "x_request_id=%s code=%s",
            operation,
            reference,
            response.status_code,
            response.headers.get("x-request-id"),
            MercadoPagoProvider._error_code(response),
        )


def verify_mercadopago_signature(
    signature: str | None,
    request_id: str | None,
    data_id: str | None,
    secret: str,
) -> bool:
    if not signature or not request_id or not data_id or not secret:
        return False

    signature_parts: dict[str, str] = {}
    for item in signature.split(","):
        key, separator, value = item.strip().partition("=")
        if separator:
            signature_parts[key] = value
    timestamp = signature_parts.get("ts")
    received_hash = signature_parts.get("v1")
    if not timestamp or not received_hash:
        return False

    normalized_data_id = data_id.lower() if data_id.isalnum() else data_id
    manifest = (
        f"id:{normalized_data_id};request-id:{request_id};ts:{timestamp};"
    )
    expected_hash = hmac.new(
        secret.encode(),
        manifest.encode(),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected_hash, received_hash)


def create_payment_provider(settings: Settings) -> PaymentProvider:
    settings.validate()
    if settings.payment_provider == "mock":
        return MockPaymentProvider(settings)
    if settings.payment_provider == "mercadopago":
        return MercadoPagoProvider(settings)
    raise RuntimeError(f"Provedor de pagamento inválido: {settings.payment_provider}")
