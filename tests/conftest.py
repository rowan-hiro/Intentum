from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from agent_backend import Backend

ORDERS_CSV = Path(__file__).resolve().parent.parent / "examples" / "orders.csv"


class FakeClock:
    """Deterministic clock; tests advance it explicitly."""

    def __init__(self, start: dt.datetime) -> None:
        self.now = start

    def __call__(self) -> dt.datetime:
        return self.now

    def advance(self, **kwargs) -> None:
        self.now = self.now + dt.timedelta(**kwargs)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(dt.datetime(2026, 8, 27, 9, 0, tzinfo=dt.timezone.utc))


@pytest.fixture
def backend(tmp_path: Path, clock: FakeClock):
    b = Backend(tmp_path / "workspace", clock=clock)
    yield b
    b.close()


@pytest.fixture
def orders(backend: Backend) -> dict:
    response = backend.import_dataset(str(ORDERS_CSV), description="Orders imported from the shop export")
    assert response["status"] == "success", response
    return response["dataset"]


def write_csv(path: Path, header: str, rows: list[str]) -> Path:
    path.write_text("\n".join([header, *rows]) + "\n")
    return path
