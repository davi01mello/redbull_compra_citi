import os
import re

import pytest
from alembic import command
from alembic.config import Config
from dotenv import load_dotenv
from sqlalchemy.engine import make_url


load_dotenv()


def validate_test_database_url(test_database_url: str | None, application_database_url: str | None) -> str:
    if not test_database_url:
        raise RuntimeError(
            "TEST_DATABASE_URL não está configurada. Os testes não usam DATABASE_URL como fallback."
        )

    try:
        test_url = make_url(test_database_url)
    except ValueError as exc:
        raise RuntimeError("TEST_DATABASE_URL é inválida") from exc

    if test_url.drivername != "postgresql+asyncpg":
        raise RuntimeError("TEST_DATABASE_URL deve usar PostgreSQL com o driver asyncpg")

    database_name = (test_url.database or "").lower()
    clearly_test_database = bool(
        re.fullmatch(r"test(?:_[a-z0-9]+)*", database_name)
        or re.fullmatch(r"[a-z0-9]+(?:_[a-z0-9]+)*_test", database_name)
    )
    if not clearly_test_database:
        raise RuntimeError(
            "O banco de TEST_DATABASE_URL deve começar com 'test' ou terminar com '_test'"
        )

    if application_database_url:
        try:
            application_url = make_url(application_database_url)
        except ValueError:
            application_url = None
        if application_url == test_url:
            raise RuntimeError("TEST_DATABASE_URL não pode apontar para o mesmo banco de DATABASE_URL")

    return test_database_url


TEST_DATABASE_URL = validate_test_database_url(
    os.getenv("TEST_DATABASE_URL"), os.getenv("DATABASE_URL")
)

# app.db.base continua lendo DATABASE_URL; somente o processo do Pytest recebe
# a URL isolada antes de importar qualquer módulo da aplicação.
os.environ["DATABASE_URL"] = TEST_DATABASE_URL
os.environ["PAYMENT_PROVIDER"] = "mock"


@pytest.fixture(scope="session", autouse=True)
def migrate_test_database() -> None:
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", TEST_DATABASE_URL)
    command.upgrade(config, "head")
