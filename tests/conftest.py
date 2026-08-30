from __future__ import annotations

from collections.abc import Callable, Iterator

import pytest
from fastapi import APIRouter

from tests.support import ApiHarness, DatabaseHarness


@pytest.fixture
def database_harness() -> Iterator[DatabaseHarness]:
    harness = DatabaseHarness()
    try:
        yield harness
    finally:
        harness.close()


@pytest.fixture
def api_harness_factory() -> Iterator[Callable[[APIRouter], ApiHarness]]:
    harnesses: list[ApiHarness] = []

    def create(router: APIRouter) -> ApiHarness:
        harness = ApiHarness(router)
        harnesses.append(harness)
        return harness

    try:
        yield create
    finally:
        for harness in harnesses:
            harness.close()
