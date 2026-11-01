"""Shared API fixtures: each test gets a fresh temporary database."""

from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.rules import RULES_DIR

FIXED_TIME = datetime(2026, 11, 1, 3, 0, 1, 123456, tzinfo=UTC)
TEST_CARD_REF_KEY = "test-card-ref-key"


def fixed_clock() -> datetime:
    return FIXED_TIME


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "decisions.db"


@pytest.fixture
def make_client(db_path: Path) -> Iterator[Callable[..., TestClient]]:
    """Start an app on the test database; later calls can change rules or clock."""
    started: list[TestClient] = []

    def make(**options: Any) -> TestClient:
        settings = {
            "rules_dir": RULES_DIR,
            "clock": fixed_clock,
            "card_ref_key": TEST_CARD_REF_KEY,
            **options,
        }
        client = TestClient(create_app(db_path, **settings))
        client.__enter__()
        started.append(client)
        return client

    yield make
    for client in started:
        client.__exit__(None, None, None)


@pytest.fixture
def client(make_client: Callable[..., TestClient]) -> TestClient:
    return make_client()
