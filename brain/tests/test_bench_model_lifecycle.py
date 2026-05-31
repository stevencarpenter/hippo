"""Tests for the vendor-neutral bench model-lifecycle abstraction.

No real network calls: httpx is patched at
`hippo_brain.bench.model_lifecycle.httpx`. The LM Studio path is verified by
patching `hippo_brain.bench.lms`.
"""

from __future__ import annotations

from unittest.mock import MagicMock, call

import httpx
import pytest

from hippo_brain.bench import model_lifecycle
from hippo_brain.bench.model_lifecycle import (
    LmsLifecycle,
    ModelLifecycleError,
    OmlxLifecycle,
    get_model_lifecycle,
)

BASE_URL = "http://localhost:8000/v1"


def _resp(status_code: int = 200, json_body: dict | None = None, text: str = "") -> MagicMock:
    r = MagicMock(name=f"Response[{status_code}]")
    r.status_code = status_code
    r.text = text
    if json_body is None:
        r.json.side_effect = ValueError("no json")
    else:
        r.json.return_value = json_body
    return r


@pytest.fixture(autouse=True)
def _no_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("HIPPO_BENCH_MODEL_LIFECYCLE", raising=False)


def test_prepare_unloads_only_other_loaded_models_then_loads_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    status = _resp(
        json_body={
            "models": [
                {"id": "target", "loaded": False},
                {"id": "other-a", "loaded": True},
                {"id": "other-b", "loaded": True},
                {"id": "cold", "loaded": False},
            ]
        }
    )
    get_mock = MagicMock(return_value=status)
    post_mock = MagicMock(return_value=_resp(json_body={"status": "ok"}))
    monkeypatch.setattr(model_lifecycle.httpx, "get", get_mock)
    monkeypatch.setattr(model_lifecycle.httpx, "post", post_mock)

    lc = OmlxLifecycle(BASE_URL)
    load_ms = lc.prepare("target")

    assert isinstance(load_ms, int)
    get_mock.assert_called_once()
    assert get_mock.call_args.args[0] == f"{BASE_URL}/models/status"

    # Unload other-a, unload other-b, then load target — in that exact order.
    posted_urls = [c.args[0] for c in post_mock.call_args_list]
    assert posted_urls == [
        f"{BASE_URL}/models/other-a/unload",
        f"{BASE_URL}/models/other-b/unload",
        f"{BASE_URL}/models/target/load",
    ]


def test_prepare_idempotent_reload_when_target_already_loaded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    status = _resp(json_body={"models": [{"id": "target", "loaded": True}]})
    monkeypatch.setattr(model_lifecycle.httpx, "get", MagicMock(return_value=status))
    post_mock = MagicMock(
        return_value=_resp(json_body={"status": "ok", "message": "Already loaded"})
    )
    monkeypatch.setattr(model_lifecycle.httpx, "post", post_mock)

    lc = OmlxLifecycle(BASE_URL)
    lc.prepare("target")

    # No unload of the target; just a (cheap) idempotent load.
    posted_urls = [c.args[0] for c in post_mock.call_args_list]
    assert posted_urls == [f"{BASE_URL}/models/target/load"]


def test_load_404_raises_with_server_message(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        model_lifecycle.httpx,
        "get",
        MagicMock(return_value=_resp(json_body={"models": []})),
    )
    not_found = _resp(
        status_code=404,
        json_body={"error": {"message": "Model not found: nope", "type": "not_found_error"}},
        text='{"error":{"message":"Model not found: nope"}}',
    )
    monkeypatch.setattr(model_lifecycle.httpx, "post", MagicMock(return_value=not_found))

    lc = OmlxLifecycle(BASE_URL)
    with pytest.raises(ModelLifecycleError, match="Model not found: nope"):
        lc.prepare("nope")


def test_unload_404_is_treated_as_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 404 on unload means the model is already gone — idempotent, not an error."""
    status = _resp(json_body={"models": [{"id": "ghost", "loaded": True}]})
    monkeypatch.setattr(model_lifecycle.httpx, "get", MagicMock(return_value=status))
    not_found = _resp(status_code=404, text="not found")
    post_mock = MagicMock(side_effect=[not_found, _resp(json_body={"status": "ok"})])
    monkeypatch.setattr(model_lifecycle.httpx, "post", post_mock)

    lc = OmlxLifecycle(BASE_URL)
    # Must not raise — unload 404 is idempotent; prepare continues to load target.
    lc.prepare("target")

    posted_urls = [c.args[0] for c in post_mock.call_args_list]
    assert posted_urls == [
        f"{BASE_URL}/models/ghost/unload",
        f"{BASE_URL}/models/target/load",
    ]


def test_connect_error_raises_model_lifecycle_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        model_lifecycle.httpx,
        "get",
        MagicMock(side_effect=httpx.ConnectError("connection refused")),
    )
    monkeypatch.setattr(model_lifecycle.httpx, "post", MagicMock())

    lc = OmlxLifecycle(BASE_URL)
    with pytest.raises(ModelLifecycleError, match="connection refused"):
        lc.prepare("target")


def test_http_500_on_status_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        model_lifecycle.httpx,
        "get",
        MagicMock(return_value=_resp(status_code=500, text="boom")),
    )
    monkeypatch.setattr(model_lifecycle.httpx, "post", MagicMock())
    lc = OmlxLifecycle(BASE_URL)
    with pytest.raises(ModelLifecycleError, match="HTTP 500"):
        lc.prepare("target")


def test_status_parsing_handles_real_response_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    status = _resp(
        json_body={
            "models": [
                {"id": "a", "loaded": True, "is_loading": False, "estimated_size": 123},
                {"id": "b", "loaded": False, "is_loading": False, "estimated_size": 456},
            ],
            "extra": "ignored",
        }
    )
    monkeypatch.setattr(model_lifecycle.httpx, "get", MagicMock(return_value=status))
    lc = OmlxLifecycle(BASE_URL)
    assert lc._loaded_model_ids() == ["a"]


def test_bearer_header_omitted_without_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    get_mock = MagicMock(return_value=_resp(json_body={"models": []}))
    monkeypatch.setattr(model_lifecycle.httpx, "get", get_mock)
    monkeypatch.setattr(model_lifecycle.httpx, "post", MagicMock(return_value=_resp(json_body={})))
    lc = OmlxLifecycle(BASE_URL)
    lc.prepare("target")
    assert get_mock.call_args.kwargs["headers"] == {}


def test_bearer_header_included_with_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "secret-123")
    get_mock = MagicMock(return_value=_resp(json_body={"models": []}))
    monkeypatch.setattr(model_lifecycle.httpx, "get", get_mock)
    monkeypatch.setattr(model_lifecycle.httpx, "post", MagicMock(return_value=_resp(json_body={})))
    lc = OmlxLifecycle(BASE_URL)
    lc.prepare("target")
    assert get_mock.call_args.kwargs["headers"] == {"Authorization": "Bearer secret-123"}


def test_get_model_lifecycle_defaults_to_omlx(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HIPPO_BENCH_MODEL_LIFECYCLE", raising=False)
    lc = get_model_lifecycle(BASE_URL)
    assert isinstance(lc, OmlxLifecycle)
    assert lc.base_url == BASE_URL


def test_get_model_lifecycle_explicit_omlx(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HIPPO_BENCH_MODEL_LIFECYCLE", "omlx")
    assert isinstance(get_model_lifecycle(BASE_URL), OmlxLifecycle)


def test_get_model_lifecycle_lms_selection(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HIPPO_BENCH_MODEL_LIFECYCLE", "lms")
    assert isinstance(get_model_lifecycle(BASE_URL), LmsLifecycle)


def test_get_model_lifecycle_unknown_backend_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HIPPO_BENCH_MODEL_LIFECYCLE", "bogus")
    with pytest.raises(ModelLifecycleError, match="bogus"):
        get_model_lifecycle(BASE_URL)


def test_lms_lifecycle_delegates_to_lms_module(monkeypatch: pytest.MonkeyPatch) -> None:
    unload_all = MagicMock()
    load = MagicMock()
    sleep = MagicMock()
    monkeypatch.setattr(model_lifecycle.lms, "unload_all", unload_all)
    monkeypatch.setattr(model_lifecycle.lms, "load", load)
    monkeypatch.setattr(model_lifecycle.time, "sleep", sleep)

    lc = LmsLifecycle()
    load_ms = lc.prepare("my-model")

    assert isinstance(load_ms, int)
    unload_all.assert_called_once_with()
    load.assert_called_once_with("my-model")
    # unload happens before load
    assert unload_all.call_args_list == [call()]
