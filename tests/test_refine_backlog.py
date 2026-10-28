"""精修队列的溢出与让行策略（2026-10-28 修改）。

背景是一场 33 分钟的连续讲课：精修线程「只要还在说话就完全不跑」，队列顶到
240 秒上限后开始丢最老的段落，那些段落永远停在流式文本——用户看到的是
「精修落后，部分句子保留实时文本」，而且那一行的「精修中」徽标永不消失。

这三条用例分别锁住：
1. 溢出丢的是最老的，并且每丢一条都要发 ``refine_skip``（否则徽标不动）；
2. 「精修落后」的横幅一次会话只发一次，但 ``refine_skip`` 每条都要发；
3. 让行策略的三档：不说话立即跑 / 队列短时继续让 / 队列越过红线不再让。

会话对象用 ``object.__new__`` 拼出来：这里测的是队列数学，不碰音频、不碰模型。
"""

from __future__ import annotations

import queue
import threading
import time

import numpy as np
import pytest

from app import config, main, protocol

SR = config.SAMPLE_RATE


class FakeSession:
    """只带 _enqueue_refine / _refine_turn 需要的那几个属性。"""

    def __init__(self, backlog_seconds: float = 240.0, monkeypatch=None) -> None:
        self.id = "test-session"
        self.epoch = 1
        self._refine = queue.Queue()
        self._refine_lock = threading.Lock()
        self._refine_pending = 0
        self._refine_seconds = 0.0
        self._refine_dropped = 0
        self._timings: dict[str, float] = {}
        self._warned_refine = False
        self._speaking = False
        self._foreground = False
        self._stop = threading.Event()
        self.emitted: list[dict] = []
        self._backlog_limit = backlog_seconds
        if monkeypatch is not None:
            monkeypatch.setattr(config, "REFINE_BACKLOG_SECONDS", backlog_seconds)

    # 绑定真实实现，避免测试里复制一份逻辑
    _enqueue_refine = main.StreamingSession._enqueue_refine
    _refine_turn = main.StreamingSession._refine_turn
    _emit_refine_skip = main.StreamingSession._emit_refine_skip

    def _emit(self, message: dict) -> None:
        protocol.validate(message)
        self.emitted.append(message)


def audio(seconds: float) -> np.ndarray:
    return np.zeros(int(seconds * SR), dtype=np.float32)


def skips(session: FakeSession) -> list[str]:
    return [m["id"] for m in session.emitted if m["type"] == "refine_skip"]


def warnings(session: FakeSession) -> list[dict]:
    return [m for m in session.emitted if m["type"] == "warning"]


def test_overflow_drops_the_oldest_and_says_so(monkeypatch):
    session = FakeSession(backlog_seconds=10.0, monkeypatch=monkeypatch)
    # 每条 4 秒；第三条进来时 8+4 > 10，必须丢掉 1-1
    for index in range(1, 4):
        session._enqueue_refine(f"1-{index}", audio(4), 0.0, 4.0)

    assert session._refine_dropped == 1
    assert skips(session) == ["1-1"]
    # 队列里剩 1-2 / 1-3，最老的先走
    remaining = []
    while not session._refine.empty():
        remaining.append(session._refine.get_nowait()[0])
    assert remaining == ["1-2", "1-3"]
    # 溢出时那条「精修落后」的横幅发过一次
    assert [m["code"] for m in warnings(session)] == ["refine_backlog_dropped"]


def test_every_dropped_segment_gets_its_own_skip(monkeypatch):
    session = FakeSession(backlog_seconds=8.0, monkeypatch=monkeypatch)
    for index in range(1, 6):
        session._enqueue_refine(f"1-{index}", audio(4), 0.0, 4.0)

    # 横幅只发一次，但每一段被丢的都要有 skip
    assert len(warnings(session)) == 1
    assert skips(session) == ["1-1", "1-2", "1-3"]
    assert session._refine_dropped == 3


def test_idle_session_refines_immediately(monkeypatch):
    session = FakeSession(monkeypatch=monkeypatch)
    slept: list[float] = []
    monkeypatch.setattr(main.time, "sleep", lambda seconds: slept.append(seconds))
    assert session._refine_turn(5.0) is True
    assert slept == []


def test_short_backlog_keeps_yielding_to_live(monkeypatch):
    """积压还在下限内：继续让行，直到会话停下（返回 False）。"""
    session = FakeSession(monkeypatch=monkeypatch)
    session._speaking = True
    session._refine_seconds = max(0.0, config.REFINE_YIELD_BELOW_S - 1)
    slept: list[float] = []

    def fake_sleep(seconds: float) -> None:
        slept.append(seconds)
        if len(slept) >= 3:
            session._stop.set()

    monkeypatch.setattr(main.time, "sleep", fake_sleep)
    assert session._refine_turn(5.0) is False
    assert slept == [0.05, 0.05, 0.05]


def test_mid_backlog_throttles_instead_of_stopping(monkeypatch):
    """积压越过分界线：睡一小段就跑，不再无限等下去。"""
    session = FakeSession(monkeypatch=monkeypatch)
    session._speaking = True
    session._refine_seconds = (config.REFINE_YIELD_BELOW_S + config.REFINE_URGENT_BACKLOG_S) / 2
    slept: list[float] = []
    monkeypatch.setattr(main.time, "sleep", lambda seconds: slept.append(seconds))

    assert session._refine_turn(5.0) is True
    assert len(slept) == 1
    assert 0 < slept[0] <= config.REFINE_THROTTLE_SLEEP_S


def test_urgent_backlog_refines_without_yielding(monkeypatch):
    session = FakeSession(monkeypatch=monkeypatch)
    session._speaking = True
    session._refine_seconds = config.REFINE_URGENT_BACKLOG_S
    slept: list[float] = []
    monkeypatch.setattr(main.time, "sleep", lambda seconds: slept.append(seconds))

    assert session._refine_turn(5.0) is True
    assert slept == []


def test_thresholds_are_ordered():
    """三段线必须自洽，否则「限速」这一段永远进不去。"""
    assert 0 <= config.REFINE_YIELD_BELOW_S < config.REFINE_URGENT_BACKLOG_S
    assert config.REFINE_URGENT_BACKLOG_S < config.REFINE_BACKLOG_SECONDS


def test_refine_skip_is_a_valid_message():
    message = protocol.refine_skip(final_id="2-7", epoch=2)
    protocol.validate(message)
    assert message == {"type": "refine_skip", "id": "2-7", "epoch": 2, "reason": "backlog"}


@pytest.mark.parametrize("bad_id", ["7", "2-0", "abc", "2-7-1"])
def test_refine_skip_rejects_malformed_ids(bad_id):
    with pytest.raises(protocol.ProtocolError):
        protocol.validate(
            {"type": "refine_skip", "id": bad_id, "epoch": 2, "reason": "backlog"}
        )


def test_malformed_id_never_breaks_the_queue(monkeypatch):
    """id 不合协议时宁可不发通知，也不能让丢弃流程抛异常卡住队列。"""
    session = FakeSession(backlog_seconds=4.0, monkeypatch=monkeypatch)
    session._enqueue_refine("garbage", audio(4), 0.0, 4.0)  # 占满队列
    session._enqueue_refine("1-2", audio(4), 0.0, 4.0)  # 溢出 → 丢 garbage

    assert session._refine_dropped == 1
    assert skips(session) == []  # id 不合协议 → 不发 skip
    assert len(warnings(session)) == 1  # 但横幅照发
    assert session._refine.qsize() == 1  # 队尾那段还在
