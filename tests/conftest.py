from __future__ import annotations

import os
import subprocess
from collections.abc import Callable, Iterator

import pytest
from fastapi import APIRouter

# 测试默认走生成式/模拟分类路径，不加载真实决策头权重；
# 决策头分层路径由 tests/test_decision_head_assessment.py 显式注入 backend 验证。
os.environ.setdefault("CLASSIFIER_BACKEND", "ollama")
LOCAL_MYSQL_TEST_DATABASE_URL = "mysql+pymysql://xling_test:test-only@127.0.0.1:13306/xling_test?charset=utf8mb4"
os.environ.setdefault("MYSQL_TEST_DATABASE_URL", LOCAL_MYSQL_TEST_DATABASE_URL)

from tests.support import ApiHarness, DatabaseHarness  # noqa: E402


@pytest.fixture(scope="session")
def mysql_test_database() -> Iterator[None]:
    """本机默认测试时自动启动隔离 MySQL，并在会话结束后清理。

    Docker 测试容器或显式提供外部测试库地址时，不管理其服务生命周期。
    """
    if os.environ["MYSQL_TEST_DATABASE_URL"] != LOCAL_MYSQL_TEST_DATABASE_URL:
        yield
        return
    command = ["docker", "compose", "--profile", "test"]
    try:
        subprocess.run(
            [*command, "up", "-d", "--wait", "mysql-test"],
            check=True, capture_output=True, text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        pytest.fail(f"无法自动启动 MySQL 测试数据库：{exc}")
    try:
        yield
    finally:
        subprocess.run([*command, "stop", "mysql-test"], check=True, capture_output=True)
        subprocess.run([*command, "rm", "-f", "mysql-test"], check=True, capture_output=True)


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
