from __future__ import annotations

import base64
import hashlib
import hmac
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx

from .config import Settings


@dataclass
class Charge:
    id: str
    correlation_id: str
    br_code: str
    expires_at: datetime
    simulated: bool


@dataclass
class ChargeStatus:
    completed: bool
    value_cents: int


class PaymentProviderError(RuntimeError):
    pass


class MockPaymentProvider:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def create_charge(self, correlation_id: str, value_cents: int) -> Charge:
        expires_at = datetime.now(UTC) + timedelta(seconds=self.settings.charge_expires_in)
        return Charge(
            id=f"SIM-{correlation_id}", correlation_id=correlation_id,
            br_code=f"00020126PIX-SIMULADO-{correlation_id}-VALOR-{value_cents}",
            expires_at=expires_at, simulated=True,
        )

    def get_charge(self, correlation_id: str) -> ChargeStatus:
        return ChargeStatus(completed=True, value_cents=0)


def _emv(tag: str, value: str) -> str:
    return f"{tag}{len(value):02d}{value}"


def _pix_text(value: str, limit: int) -> str:
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    return normalized.upper()[:limit]


def _crc16(payload: str) -> str:
    result = 0xFFFF
    for byte in payload.encode("utf-8"):
        result ^= byte << 8
        for _ in range(8):
            result = ((result << 1) ^ 0x1021) & 0xFFFF if result & 0x8000 else (result << 1) & 0xFFFF
    return f"{result:04X}"


def build_static_pix_code(key: str, receiver_name: str, receiver_city: str, value_cents: int, txid: str) -> str:
    merchant = _emv("00", "br.gov.bcb.pix") + _emv("01", key)
    additional = _emv("05", _pix_text(txid.replace("-", ""), 25) or "***")
    payload = "".join([
        _emv("00", "01"), _emv("26", merchant), _emv("52", "0000"), _emv("53", "986"),
        _emv("54", f"{value_cents / 100:.2f}"), _emv("58", "BR"),
        _emv("59", _pix_text(receiver_name, 25)), _emv("60", _pix_text(receiver_city, 15)),
        _emv("62", additional), "6304",
    ])
    return payload + _crc16(payload)


class StaticPixProvider:
    """PIX real temporário. Recebe dinheiro, mas não oferece confirmação automática."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        settings.validate()

    def create_charge(self, correlation_id: str, value_cents: int) -> Charge:
        return Charge(
            id=f"STATIC-{correlation_id}", correlation_id=correlation_id,
            br_code=build_static_pix_code(
                self.settings.pix_key, self.settings.pix_receiver_name,
                self.settings.pix_receiver_city, value_cents, correlation_id,
            ),
            expires_at=datetime.now(UTC) + timedelta(days=1), simulated=False,
        )

    def get_charge(self, correlation_id: str) -> ChargeStatus:
        raise PaymentProviderError("PIX estático não possui confirmação automática")


class OpenPixProvider:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": self.settings.openpix_app_id, "Content-Type": "application/json", "User-Agent": "CITi-RedBull-PDV/1.0"}

    def create_charge(self, correlation_id: str, value_cents: int) -> Charge:
        payload = {"value": value_cents, "correlationID": correlation_id, "expiresIn": self.settings.charge_expires_in}
        try:
            with httpx.Client(timeout=15) as client:
                response = client.post(f"{self.settings.openpix_base_url}/api/v1/charge", headers=self.headers, json=payload)
                response.raise_for_status()
                data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            recovered = self._recover_charge(correlation_id)
            if recovered:
                return recovered
            raise PaymentProviderError("Não foi possível criar a cobrança PIX") from exc
        return self._parse_charge(data, correlation_id)

    def _recover_charge(self, correlation_id: str) -> Charge | None:
        try:
            with httpx.Client(timeout=10) as client:
                response = client.get(f"{self.settings.openpix_base_url}/api/v1/charge/{correlation_id}", headers=self.headers)
                if response.status_code == 404:
                    return None
                response.raise_for_status()
                return self._parse_charge(response.json(), correlation_id)
        except (httpx.HTTPError, ValueError, PaymentProviderError):
            return None

    def _parse_charge(self, data: dict, correlation_id: str) -> Charge:
        charge = data.get("charge", data)
        br_code = data.get("brCode") or charge.get("brCode")
        charge_id = charge.get("globalID") or charge.get("paymentLinkID") or charge.get("transactionID") or correlation_id
        if not br_code:
            raise PaymentProviderError("A Woovi não retornou o código PIX")
        expires_raw = charge.get("expiresDate")
        try:
            expires_at = datetime.fromisoformat(expires_raw.replace("Z", "+00:00")) if expires_raw else datetime.now(UTC) + timedelta(seconds=self.settings.charge_expires_in)
        except (TypeError, ValueError):
            expires_at = datetime.now(UTC) + timedelta(seconds=self.settings.charge_expires_in)
        return Charge(id=charge_id, correlation_id=correlation_id, br_code=br_code, expires_at=expires_at, simulated=False)

    def get_charge(self, correlation_id: str) -> ChargeStatus:
        try:
            with httpx.Client(timeout=10) as client:
                response = client.get(f"{self.settings.openpix_base_url}/api/v1/charge/{correlation_id}", headers=self.headers)
                response.raise_for_status()
                data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise PaymentProviderError("Não foi possível confirmar a cobrança na Woovi") from exc
        charge = data.get("charge", data)
        return ChargeStatus(completed=charge.get("status") == "COMPLETED", value_cents=int(charge.get("value", 0)))


def verify_openpix_signature(raw_body: bytes, signature: str | None, secret: str) -> bool:
    if not signature or not secret:
        return False
    digest = hmac.new(secret.encode(), raw_body, hashlib.sha1).digest()
    expected = base64.b64encode(digest).decode()
    return hmac.compare_digest(expected, signature)


def create_payment_provider(settings: Settings):
    if settings.payment_provider == "openpix":
        settings.validate()
        return OpenPixProvider(settings)
    if settings.payment_provider == "static_pix":
        return StaticPixProvider(settings)
    return MockPaymentProvider(settings)
