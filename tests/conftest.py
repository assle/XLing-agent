from __future__ import annotations

from collections.abc import Callable, Iterator

import pytest
from fastapi import APIRouter

from tests.support import ApiHarness, DatabaseHarness


@pytest.fixture
def database_harness() -> Iterator[DatabaseHarness]:
    """为一个测试提供独立内存数据库环境，并在测试结束后清理。

    yield 前建立表结构，finally 无论断言成功失败都会释放测试资源。
    """
    harness = DatabaseHarness()
    try:
        yield harness
    finally:
        harness.close()


@pytest.fixture
def api_harness_factory() -> Iterator[Callable[[APIRouter], ApiHarness]]:
    """为测试提供按接口创建本地测试应用的工厂。

    记录工厂创建的所有环境，测试结束后逐个清理数据库。
    """
    harnesses: list[ApiHarness] = []

    def create(router: APIRouter) -> ApiHarness:
        """为传入接口创建测试应用，并登记到外层清理列表。

        返回可发请求的环境对象，避免每个测试重复配置数据库依赖。
        """
        harness = ApiHarness(router)
        harnesses.append(harness)
        return harness

    try:
        yield create
    finally:
        for harness in harnesses:
            harness.close()
