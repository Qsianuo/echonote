"""HTTP surface: liveness vs readiness, config, security headers, bind rules."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import config, main, stt


@pytest.fixture()
def client():
    with TestClient(main.app) as test_client:
        yield test_client


def test_health_is_liveness_only(client):
    response = client.get("/api/health")
    assert response.status_code == 200
    # FR-044: health must not imply anything about the model.
    assert response.json() == {"ok": True}


def test_ready_exposes_model_state(client):
    body = client.get("/api/ready").json()
    for key in ("ready", "model", "error", "retrying"):
        assert key in body
    assert client.get("/api/status").json().keys() == body.keys()


def test_config_matches_the_documented_limits(client):
    body = client.get("/api/config").json()

    assert body["protocol"] == "2"
    assert body["version"] == config.APP_VERSION
    assert body["sample_rate"] == 16000
    assert len(body["languages"]) == 9
    assert body["language"] == "zh"
    assert body["max_frame_bytes"] == 32768
    assert body["min_frame_ms"] == 100
    assert body["max_record_minutes"] == 90
    assert body["max_sessions"] == 2
    assert body["queue_max_seconds"] == 60


def test_security_headers_are_present(client):
    headers = client.get("/api/health").headers
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["Referrer-Policy"] == "no-referrer"
    assert headers["X-Frame-Options"] == "DENY"


def test_index_is_served_with_the_app_scripts(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "EchoNote" in response.text
    assert "export.js" in response.text and "wav.js" in response.text


def test_retry_endpoint_delegates_to_the_engine(client, monkeypatch):
    monkeypatch.setattr(
        stt, "retry", lambda: {"ready": False, "model": "small", "error": "boom", "retrying": True}
    )
    body = client.post("/api/retry").json()
    assert body["retrying"] is True
    assert body["error"] == "boom"


def test_loopback_hosts_are_allowed():
    for host in ("127.0.0.1", "localhost", "::1", "127.0.0.5"):
        main.check_bind(host)  # must not raise


def test_non_loopback_bind_is_refused_by_default(monkeypatch):
    monkeypatch.setattr(config, "ALLOW_NONLOCAL", False)
    with pytest.raises(SystemExit):
        main.check_bind("0.0.0.0")


def test_non_loopback_without_token_is_refused(monkeypatch):
    monkeypatch.setattr(config, "ALLOW_NONLOCAL", True)
    monkeypatch.setattr(config, "ACCESS_TOKEN", "")
    with pytest.raises(SystemExit):
        main.check_bind("192.168.1.10")


def test_non_loopback_with_token_is_allowed(monkeypatch):
    monkeypatch.setattr(config, "ALLOW_NONLOCAL", True)
    monkeypatch.setattr(config, "ACCESS_TOKEN", "s3cret")
    main.check_bind("192.168.1.10")  # must not raise


def test_origin_allowlist():
    assert main._origin_allowed(None) is True
    assert main._origin_allowed("http://127.0.0.1:8000") is True
    assert main._origin_allowed("http://localhost:5173") is True
    assert main._origin_allowed("https://evil.example") is False


# --------------------------------------------------------------------------
# FR-047: model inventory and switching
# --------------------------------------------------------------------------


def test_models_lists_the_catalog_with_install_state(client):
    body = client.get("/api/models").json()
    assert body["current"]
    keys = [item["key"] for item in body["items"]]
    assert "large-v3-turbo" in keys
    assert "medium" in keys
    for item in body["items"]:
        assert set(item) >= {"key", "label", "size_mb", "tier", "installed"}


def test_switching_to_a_missing_model_is_rejected_with_a_path(client):
    response = client.post("/api/model", json={"key": "large-v3"})
    assert response.status_code == 400
    body = response.json()
    assert body["ok"] is False
    # The message must say where the files belong, not just "failed".
    assert "faster-whisper-large-v3" in body["error"]


def test_switching_without_a_key_is_a_client_error(client):
    response = client.post("/api/model", json={})
    assert response.status_code == 400
    assert response.json()["ok"] is False
