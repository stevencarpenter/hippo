from unittest.mock import MagicMock, patch

from hippo_brain.bench.enrich_call import (
    build_prompt,
    call_enrichment,
    call_embedding,
)


def test_build_prompt_includes_payload():
    p = build_prompt("ls -la", source="shell")
    assert "ls -la" in p
    assert "shell" in p.lower()


def test_build_prompt_differs_per_source():
    p_shell = build_prompt("x", "shell")
    p_claude = build_prompt("x", "claude")
    assert p_shell != p_claude


def test_call_enrichment_returns_timing_and_raw():
    fake_resp = MagicMock()
    fake_resp.status_code = 200
    fake_resp.json.return_value = {"choices": [{"message": {"content": '{"summary":"ok"}'}}]}
    fake_resp.raise_for_status = MagicMock()
    with patch("httpx.post", return_value=fake_resp):
        r = call_enrichment(
            base_url="http://localhost:1234/v1",
            model="m1",
            payload="ls -la",
            source="shell",
            timeout_sec=60,
        )
    assert r.raw_output == '{"summary":"ok"}'
    assert r.total_ms >= 0
    assert r.timeout is False


def test_call_enrichment_records_timeout():
    import httpx

    with patch("httpx.post", side_effect=httpx.TimeoutException("slow")):
        r = call_enrichment(
            base_url="http://localhost:1234/v1",
            model="m1",
            payload="x",
            source="shell",
            timeout_sec=1,
        )
    assert r.timeout is True
    assert r.raw_output == ""


def test_call_embedding_returns_vector():
    fake_resp = MagicMock()
    fake_resp.status_code = 200
    fake_resp.json.return_value = {"data": [{"embedding": [0.1, 0.2, 0.3]}]}
    fake_resp.raise_for_status = MagicMock()
    with patch("httpx.post", return_value=fake_resp):
        v = call_embedding(
            base_url="http://localhost:1234/v1",
            model="nomic-embed-text",
            text="hello",
            timeout_sec=60,
        )
    assert v == [0.1, 0.2, 0.3]
