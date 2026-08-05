from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Protocol

import httpx

from .config import Settings


MERCADOPAGO_API_URL = "https://api.mercadopago.com"
MERCADOPAGO_FALLBACK_PAYER_EMAIL = "vendas@citi.local"


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
    provider_payment_id: str
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
    ) -> ChargeResult: ...

    async def get_payment(self, payment_id: str) -> PaymentResult: ...

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
        self._payments: dict[str, PaymentResult] = {}

    async def create_charge(
        self,
        external_reference: str,
        amount: Decimal,
        idempotency_key: str,
    ) -> ChargeResult:
        if not idempotency_key:
            raise PaymentProviderError("A chave de idempotência é obrigatória")
        provider_order_id = f"MOCK-ORDER-{external_reference}"
        provider_payment_id = f"MOCK-PAYMENT-{external_reference}"
        expires_at = datetime.now(UTC) + timedelta(
            seconds=self.settings.charge_expires_in
        )
        self._payments[provider_payment_id] = PaymentResult(
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

    async def get_payment(self, payment_id: str) -> PaymentResult:
        try:
            return self._payments[payment_id]
        except KeyError as exc:
            raise PaymentProviderError("Pagamento mock não encontrado") from exc

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
    ) -> ChargeResult:
        if not idempotency_key:
            raise PaymentProviderError("A chave de idempotência é obrigatória")

        amount_text = f"{amount:.2f}"
        expires_at = datetime.now(UTC) + timedelta(
            seconds=self.settings.charge_expires_in
        )
        payer = {"email": MERCADOPAGO_FALLBACK_PAYER_EMAIL}
        if self.settings.mercadopago_test_payer_first_name:
            payer["first_name"] = self.settings.mercadopago_test_payer_first_name
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
            "payer": payer,
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
            raise PaymentProviderError(
                "Tempo esgotado ao criar a cobrança no Mercado Pago"
            ) from exc
        except httpx.HTTPStatusError as exc:
            detail = self._error_detail(exc.response)
            raise PaymentProviderError(
                f"Mercado Pago recusou a cobrança: {detail}"
            ) from exc
        except (httpx.TransportError, ValueError) as exc:
            raise PaymentProviderError(
                "Resposta inválida ao criar a cobrança no Mercado Pago"
            ) from exc

        return self._parse_charge(data, amount, expires_at)

    async def get_payment(self, payment_id: str) -> PaymentResult:
        try:
            response = await self._client.get(
                f"/v1/payments/{payment_id}",
                headers=self._headers,
            )
            response.raise_for_status()
            data = response.json()
        except httpx.TimeoutException as exc:
            raise PaymentProviderError(
                "Tempo esgotado ao consultar o pagamento no Mercado Pago"
            ) from exc
        except httpx.HTTPStatusError as exc:
            detail = self._error_detail(exc.response)
            raise PaymentProviderError(
                f"Mercado Pago recusou a consulta: {detail}"
            ) from exc
        except (httpx.TransportError, ValueError) as exc:
            raise PaymentProviderError(
                "Resposta inválida ao consultar o pagamento no Mercado Pago"
            ) from exc

        if not isinstance(data, dict):
            raise PaymentProviderError("O Mercado Pago retornou uma resposta inválida")
        provider_payment_id = data.get("id")
        if provider_payment_id is None or not data.get("status"):
            raise PaymentProviderError(
                "O Mercado Pago retornou um pagamento incompleto"
            )
        try:
            amount = Decimal(str(data["transaction_amount"]))
        except (KeyError, InvalidOperation, TypeError, ValueError) as exc:
            raise PaymentProviderError(
                "O Mercado Pago retornou um valor de pagamento inválido"
            ) from exc
        return PaymentResult(
            provider_payment_id=str(provider_payment_id),
            status=str(data["status"]).upper(),
            amount=amount,
            currency=str(data.get("currency_id") or "BRL"),
            approved_at=_parse_datetime(data.get("date_approved")),
        )

    async def aclose(self) -> None:
        await self._client.aclose()

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
    def _error_detail(response: httpx.Response) -> str:
        try:
            data = response.json()
        except ValueError:
            return str(response.status_code)
        if not isinstance(data, dict):
            return str(response.status_code)
        return str(
            data.get("message")
            or data.get("error")
            or data.get("details")
            or response.status_code
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
