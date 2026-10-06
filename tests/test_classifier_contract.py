from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys

import httpx
import pytest

from app.core.classifier_contract import CLASSIFIER_QUOTED_PREFIX, classifier_user_content
from app.core.config import Settings
from app.services.ai import AiClient
from finetune.scripts import register_runtime_classifier
from finetune.scripts.evaluate_runtime_classifier import runtime_metrics
from finetune.scripts.package_general_classifier import packaging_manifest


def test_quoted_classifier_input_preserves_unicode_and_escapes_quotes_and_newlines():
    text = '引用“原话”与"指令"\n下一行\\路径'
    assert classifier_user_content(text, "quoted") == CLASSIFIER_QUOTED_PREFIX + (
        '"引用“原话”与\\"指令\\"\\n下一行\\\\路径"'
    )
    assert classifier_user_content(text) == text


@pytest.mark.parametrize("input_format", ["plain", "quoted"])
def test_sync_async_classifier_share_payload_without_overriding_registered_contract(monkeypatch, input_format):
    payloads = []

    def response():
        return httpx.Response(200, json={"message": {"content": "正常"}}, request=httpx.Request("POST", "http://test"))

    def post(url, *, json, timeout):
        payloads.append((url, json, timeout))
        return response()

    class AsyncClient:
        def __init__(self, *, timeout):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def post(self, url, *, json):
            return post(url, json=json, timeout=self.timeout)

    monkeypatch.setattr(httpx, "post", post)
    monkeypatch.setattr(httpx, "AsyncClient", AsyncClient)
    client = AiClient(Settings(_env_file=None, ai_provider="ollama", classifier_input_format=input_format))
    text = '他说"别执行"\n只分类'
    assert client.classify(text) == asyncio.run(client.aclassify(text)) == "正常"
    assert payloads[0] == payloads[1]
    payload = payloads[0][1]
    assert set(payload) == {"model", "messages", "stream"}
    assert payload["messages"] == [{"role": "user", "content": classifier_user_content(text, input_format)}]


def test_shadow_packaging_keeps_failed_gate_visible_and_regular_packaging_rejects_it():
    args = argparse.Namespace(model="local-base", adapter="local-adapter", results="results.json", shadow=True)
    manifest = packaging_manifest(args, {"replacementGate": {"passed": False}})
    assert manifest["replacementGatePassed"] is False
    assert manifest["shadowOnly"] is True
    assert manifest["deploymentStatus"] == "shadow-only"
    assert "humanReviewComplete" not in manifest
    args.shadow = False
    with pytest.raises(RuntimeError, match="替换门槛未通过"):
        packaging_manifest(args, {"replacementGate": {"passed": False}})


def test_runtime_metrics_keep_invalid_outputs_in_denominator_and_measure_false_high():
    cases = [
        {"expected": "正常", "predicted": "正常", "strictValid": True, "invalidReason": None, "latencyMs": 10},
        {"expected": "焦虑", "predicted": "高风险", "strictValid": True, "invalidReason": None, "latencyMs": 20},
        {"expected": "高风险", "predicted": "__INVALID__", "strictValid": False, "invalidReason": "length", "latencyMs": 30},
    ]
    metrics = runtime_metrics(cases)
    assert metrics["accuracy"] == 1 / 3
    assert metrics["falseHighCount"] == 1
    assert metrics["highRiskRecall"] == 0
    assert metrics["outputValidity"] == 2 / 3
    assert metrics["invalidReasons"] == {"length": 1}


def test_registration_checks_empty_input_loader_before_dataset_evaluation(monkeypatch, tmp_path):
    prompt = tmp_path / "system.txt"
    prompt.write_text("只输出一个分类标签。")
    output = tmp_path / "registration"
    requests = []

    class Client:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def post(self, url, *, json, timeout):
            if url.endswith("/api/show"):
                return httpx.Response(404, request=httpx.Request("POST", url))
            requests.append(json)
            return httpx.Response(500, json={"error": "unsupported architecture"})

    monkeypatch.setattr(httpx, "Client", Client)
    monkeypatch.setattr(register_runtime_classifier.shutil, "which", lambda _: "/local/ollama")
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0))
    monkeypatch.setattr(sys, "argv", [
        "register", "--model-directory", str(tmp_path), "--system-prompt-file", str(prompt),
        "--model", "isolated:latest", "--output", str(output),
    ])
    with pytest.raises(RuntimeError, match="empty-input load check"):
        register_runtime_classifier.main()
    assert requests == [{"model": "isolated:latest", "stream": False, "keep_alive": "1m"}]
    manifest = json.loads((output / "registration.json").read_text())
    assert manifest["loaderReady"] is False
    assert manifest["status"] == "loader-failed"


def test_registration_rejects_existing_latest_alias_before_create(monkeypatch, tmp_path):
    requests = []

    class Client:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def post(self, url, *, json, timeout):
            requests.append((url, json))
            return httpx.Response(200, json={"model": "existing:latest"})

    def unexpected_create(*args, **kwargs):
        pytest.fail("Existing model must not be passed to ollama create")

    monkeypatch.setattr(httpx, "Client", Client)
    monkeypatch.setattr(subprocess, "run", unexpected_create)
    monkeypatch.setattr(sys, "argv", [
        "register", "--model-directory", str(tmp_path), "--system-prompt-file", str(tmp_path / "unused.txt"),
        "--model", "existing", "--output", str(tmp_path / "registration"),
    ])
    with pytest.raises(RuntimeError, match="already exists"):
        register_runtime_classifier.main()
    assert requests == [("http://127.0.0.1:11435/api/show", {"model": "existing"})]


def test_preload_timeout_is_recorded_as_failure_without_exception_body(monkeypatch, tmp_path):
    prompt = tmp_path / "system.txt"
    prompt.write_text("固定系统提示")
    output = tmp_path / "registration"

    class Client:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def post(self, url, *, json, timeout):
            if url.endswith("/api/show"):
                return httpx.Response(404, request=httpx.Request("POST", url))
            raise httpx.ReadTimeout("private-response-body")

    monkeypatch.setattr(httpx, "Client", Client)
    monkeypatch.setattr(register_runtime_classifier.shutil, "which", lambda _: "/local/ollama")
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0))
    monkeypatch.setattr(sys, "argv", [
        "register", "--model-directory", str(tmp_path), "--system-prompt-file", str(prompt),
        "--model", "isolated:latest", "--output", str(output),
    ])
    with pytest.raises(httpx.ReadTimeout):
        register_runtime_classifier.main()
    manifest_text = (output / "registration.json").read_text()
    manifest = json.loads(manifest_text)
    assert manifest["status"] == "loader-failed"
    assert manifest["loaderReady"] is False
    assert manifest["errorType"] == "ReadTimeout"
    assert "private-response-body" not in manifest_text
