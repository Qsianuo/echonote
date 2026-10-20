"""RG-05 / RG-08 (protocol side): websocket handshake, epoch passthrough,
frame limits and the concurrency cap.

A fake transcriber keeps the suite offline while still exercising the real
session worker, VAD and protocol plumbing.
"""

from __future__ import annotations

import json
import time

import numpy as np
import pytest
from fastapi.testclient import TestClient

from app import config, main, stt
from conftest import FRAME, speech


def wait_until_idle(timeout: float = 5.0) -> None:
    """TestClient can close a socket before the endpoint's finally block has
    released its concurrency slot; wait for that instead of racing it."""
    deadline = time.monotonic() + timeout
    while main.active_sessions() and time.monotonic() < deadline:
        time.sleep(0.02)


@pytest.fixture()
def client():
    with TestClient(main.app) as test_client:
        wait_until_idle()
        yield test_client


@pytest.fixture()
def fake_stt(monkeypatch):
    monkeypatch.setattr(stt, "is_ready", lambda: True)
    monkeypatch.setattr(
        stt, "transcribe", lambda audio, language=None, fast=False: "测试字幕"
    )


def send_speech(ws, seconds: float = 1.0) -> None:
    samples = (speech(seconds) * 32767).astype(np.int16)
    for start in range(0, len(samples) - FRAME + 1, FRAME):
        ws.send_bytes(samples[start : start + FRAME].tobytes())


def receive_until(ws, kind: str, limit: int = 30) -> dict:
    for _ in range(limit):
        message = ws.receive_json()
        if message.get("type") == kind:
            return message
    raise AssertionError(f"no {kind} message within {limit} frames")


def test_handshake_announces_protocol_v2(client):
    with client.websocket_connect("/ws") as ws:
        message = ws.receive_json()
        assert message["type"] == "status"
        assert message["protocol"] == "2"
        assert "ready" in message and "retrying" in message


def test_final_carries_the_client_epoch(client, fake_stt):
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()
        ws.send_text(
            json.dumps(
                {"type": "config", "protocol": "2", "language": "zh", "epoch": 2, "origin_ms": 25000}
            )
        )
        send_speech(ws, 1.0)
        ws.send_text(json.dumps({"type": "flush"}))

        final = receive_until(ws, "final")
        assert final["epoch"] == 2
        assert final["id"] == "2-1"
        assert final["text"] == "测试字幕"
        assert final["start"] >= 0
        assert final["end"] >= final["start"]


def test_new_epoch_keeps_ids_unique(client, fake_stt):
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()
        ws.send_text(json.dumps({"type": "config", "protocol": "2", "epoch": 3, "origin_ms": 0}))
        send_speech(ws, 1.0)
        ws.send_text(json.dumps({"type": "flush"}))
        first = receive_until(ws, "final")

        ws.send_text(json.dumps({"type": "config", "protocol": "2", "epoch": 4, "origin_ms": 30000}))
        send_speech(ws, 1.0)
        ws.send_text(json.dumps({"type": "flush"}))
        second = receive_until(ws, "final")

        assert first["id"].startswith("3-")
        assert second["id"].startswith("4-")
        assert first["id"] != second["id"]


def test_oversized_frame_is_rejected(client):
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()
        ws.send_bytes(b"\x00" * (config.MAX_FRAME_BYTES + 2))
        message = ws.receive_json()
        assert message["type"] == "error"
        assert message["code"] == "frame_too_large"
        assert message["retryable"] is False


def test_audio_before_model_is_ready_warns(client, monkeypatch):
    monkeypatch.setattr(stt, "is_ready", lambda: False)
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()
        ws.send_bytes(b"\x00" * 3200)
        message = ws.receive_json()
        assert message["type"] == "error"
        assert message["code"] == "model_not_ready"
        assert message["retryable"] is True


def test_third_concurrent_session_gets_busy(client):
    with client.websocket_connect("/ws") as first, client.websocket_connect("/ws") as second:
        assert first.receive_json()["type"] == "status"
        assert second.receive_json()["type"] == "status"

        with client.websocket_connect("/ws") as third:
            message = third.receive_json()
            assert message["type"] == "error"
            assert message["code"] == "busy"


def test_unsupported_protocol_is_reported(client, fake_stt):
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()
        ws.send_text(json.dumps({"type": "config", "protocol": "1", "language": "zh"}))
        message = ws.receive_json()
        assert message["type"] == "error"
        assert message["code"] == "unsupported_protocol"
