import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    database_url: str = os.getenv("DATABASE_URL", "sqlite:///./redbull.db")
    payment_provider: str = os.getenv("PAYMENT_PROVIDER", "mock").lower()
    openpix_environment: str = os.getenv("OPENPIX_ENVIRONMENT", "sandbox").lower()
    openpix_app_id: str = os.getenv("OPENPIX_APP_ID", "")
    openpix_webhook_secret: str = os.getenv("OPENPIX_WEBHOOK_SECRET", "")
    charge_expires_in: int = int(os.getenv("CHARGE_EXPIRES_IN", "600"))
    pix_key: str = os.getenv("PIX_KEY", "")
    pix_receiver_name: str = os.getenv("PIX_RECEIVER_NAME", "")
    pix_receiver_city: str = os.getenv("PIX_RECEIVER_CITY", "")

    @property
    def openpix_base_url(self) -> str:
        if self.openpix_environment == "production":
            return "https://api.openpix.com.br"
        return "https://api.woovi-sandbox.com"

    def validate(self) -> None:
        if self.payment_provider == "openpix":
            missing = [
                name
                for name, value in {
                    "OPENPIX_APP_ID": self.openpix_app_id,
                    "OPENPIX_WEBHOOK_SECRET": self.openpix_webhook_secret,
                }.items()
                if not value
            ]
            if missing:
                raise RuntimeError(f"Configurações obrigatórias ausentes ou inseguras: {', '.join(missing)}")
        if self.payment_provider == "static_pix":
            missing = [name for name, value in {
                "PIX_KEY": self.pix_key,
                "PIX_RECEIVER_NAME": self.pix_receiver_name,
                "PIX_RECEIVER_CITY": self.pix_receiver_city,
            }.items() if not value]
            if missing:
                raise RuntimeError(f"Configurações obrigatórias ausentes: {', '.join(missing)}")


settings = Settings()
