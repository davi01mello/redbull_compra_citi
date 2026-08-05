import os
from dataclasses import dataclass

from dotenv import load_dotenv


load_dotenv()

SUPPORTED_PAYMENT_PROVIDERS = {"mock", "mercadopago"}


@dataclass(frozen=True)
class Settings:
    mercadopago_access_token: str
    mercadopago_webhook_secret: str
    payment_provider: str
    charge_expires_in: int
    payment_expiration_check_interval: int = 60
    mercadopago_test_payer_first_name: str | None = None

    @classmethod
    def from_env(cls) -> "Settings":
        raw_expiration = os.getenv("CHARGE_EXPIRES_IN", "1800")
        try:
            charge_expires_in = int(raw_expiration)
        except ValueError as exc:
            raise RuntimeError("CHARGE_EXPIRES_IN deve ser um número inteiro") from exc

        raw_check_interval = os.getenv("PAYMENT_EXPIRATION_CHECK_INTERVAL", "60")
        try:
            payment_expiration_check_interval = int(raw_check_interval)
        except ValueError as exc:
            raise RuntimeError(
                "PAYMENT_EXPIRATION_CHECK_INTERVAL deve ser um número inteiro"
            ) from exc

        return cls(
            mercadopago_access_token=os.getenv("MERCADOPAGO_ACCESS_TOKEN", "").strip(),
            mercadopago_webhook_secret=os.getenv(
                "MERCADOPAGO_WEBHOOK_SECRET", ""
            ).strip(),
            payment_provider=os.getenv("PAYMENT_PROVIDER", "").strip().lower(),
            charge_expires_in=charge_expires_in,
            payment_expiration_check_interval=payment_expiration_check_interval,
            mercadopago_test_payer_first_name=(
                os.getenv("MERCADOPAGO_TEST_PAYER_FIRST_NAME", "").strip() or None
            ),
        )

    def validate(self) -> None:
        if self.payment_provider not in SUPPORTED_PAYMENT_PROVIDERS:
            allowed = ", ".join(sorted(SUPPORTED_PAYMENT_PROVIDERS))
            raise RuntimeError(f"PAYMENT_PROVIDER deve ser uma destas opções: {allowed}")
        if self.charge_expires_in <= 0:
            raise RuntimeError("CHARGE_EXPIRES_IN deve ser maior que zero")
        if self.payment_expiration_check_interval <= 0:
            raise RuntimeError(
                "PAYMENT_EXPIRATION_CHECK_INTERVAL deve ser maior que zero"
            )
        if self.payment_provider == "mercadopago":
            missing = [
                name
                for name, value in {
                    "MERCADOPAGO_ACCESS_TOKEN": self.mercadopago_access_token,
                    "MERCADOPAGO_WEBHOOK_SECRET": self.mercadopago_webhook_secret,
                }.items()
                if not value
            ]
            if missing:
                raise RuntimeError(
                    f"Configurações obrigatórias ausentes: {', '.join(missing)}"
                )
            if not 1800 <= self.charge_expires_in <= 2_592_000:
                raise RuntimeError(
                    "CHARGE_EXPIRES_IN para Mercado Pago deve ficar entre "
                    "1800 segundos (30 minutos) e 2592000 segundos (30 dias)"
                )


settings = Settings.from_env()
