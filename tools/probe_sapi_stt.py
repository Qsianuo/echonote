"""Probe SAPI (System.Speech) file recognition on a real WAV.

SAPI is the classic on-board Windows speech engine. It can recognize straight
from a WAV file via SpFileStream, which is exactly the "no model, offline"
path we are evaluating. This probe prints the actual transcript and timing.

Run:  python tools/probe_sapi_stt.py [path-to-wav]
"""
from __future__ import annotations

import sys
import threading
import time
import wave

import comtypes.client


def main() -> int:
    path = sys.argv[1] if len(sys.argv) > 1 else "tests/sample.wav"

    # Generate the SAPI type library once.
    comtypes.client.GetModule("C:\\Windows\\System32\\Speech\\Common\\sapi.dll")
    import comtypes.gen.SpeechLib as sl

    with wave.open(path, "rb") as w:
        frames = w.getnframes()
        rate = w.getframerate()
        duration = frames / rate
    print(f"audio: {path}  {duration:.2f}s  {rate} Hz")

    # 1) In-proc recognizer + dictation grammar.
    engine = comtypes.client.CreateObject("SAPI.SpInProcRecoContext")
    print("engine created:", type(engine).__name__)

    # Dictation grammar = free-form speech.
    grammar = engine.CreateGrammar()
    grammar.DictationSetState(sl.SGDSActive)

    # 2) File stream as audio input.
    stream = comtypes.client.CreateObject("SAPI.SpFileStream")
    stream.Open(path, sl.SSFMOpenForRead)

    # 3) Wire the recognizer's audio input to the file stream.
    reco = engine.Recognizer
    reco.AudioInputStream = stream
    print("audio input stream wired OK")

    # 4) Result handler.
    results: list[str] = []
    done = threading.Event()

    class Sink:
        def Recognition(self, stream_number, stream_position, result):
            results.append(result.PhraseInfo.GetText())
            done.set()

    conn = None
    try:
        import comtypes.gen.SpeechLib as _sl
        sink = Sink()
        conn = comtypes.client.GetEvents(engine, sink)
        print("event binding OK:", conn is not None)
    except Exception as exc:
        print(f"[WARN] event binding: {type(exc).__name__}: {exc}")

    # Activate the recognizer engine state (SRSActive = 1).
    try:
        reco.State = sl.SRSActive
        print("recognizer state:", reco.State)
    except Exception as exc:
        print(f"[WARN] state activation: {type(exc).__name__}: {exc}")

    # 5) Pump messages until a result arrives (comtypes ships its own pump).
    t0 = time.perf_counter()
    deadline = t0 + duration + 15
    while not done.is_set() and time.perf_counter() < deadline:
        comtypes.client.PumpEvents(0.02)

    elapsed = time.perf_counter() - t0
    print(f"\n=== RESULT ({elapsed:.2f}s) ===")
    if results:
        print("transcript:", " / ".join(results))
        print("VERDICT: SAPI recognizes this WAV offline")
    else:
        print("no result within timeout")
        print("VERDICT: engine loads but did not return text (needs more work)")


if __name__ == "__main__":
    raise SystemExit(main())
