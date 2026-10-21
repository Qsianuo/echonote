"""Probe Windows built-in speech recognition on a real WAV file.

Feeds tests/sample.wav through Windows.Media.SpeechRecognition (the "no model"
path) and prints the actual transcript. This is the decisive test for whether
the system engine can replace Whisper on this machine.

Run:  python tools/probe_system_stt.py [path-to-wav]
"""
from __future__ import annotations

import asyncio
import sys
import time
import wave

import numpy as np


def load_wav(path: str) -> tuple[np.ndarray, int]:
    with wave.open(path, "rb") as w:
        raw = w.readframes(w.getnframes())
        return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0, w.getframerate()


async def probe(path: str) -> None:
    import winrt.windows.media.speechrecognition as sr
    import winrt.windows.storage as storage
    import winrt.windows.storage.streams as streams

    audio, rate = load_wav(path)
    duration = len(audio) / rate
    print(f"audio: {path}  {duration:.2f}s  {rate} Hz")

    # 1) Languages.
    try:
        topics = [str(x) for x in sr.SpeechRecognizer.supported_topic_languages]
        print(f"supported topic languages: {topics}")
    except Exception as exc:
        print(f"[WARN] topic languages: {exc}")

    # 2) Recognizer + dictation constraint.
    recognizer = sr.SpeechRecognizer()
    print(f"current_language: {recognizer.current_language}")
    print(f"state (before): {recognizer.state}")

    t0 = time.perf_counter()
    compiled = await recognizer.compile_constraints_async()
    print(f"compile: status={compiled.status} ({(time.perf_counter() - t0) * 1000:.0f} ms)")
    if compiled.status != 0:
        print("[FAIL] constraints did not compile")
        return

    # 3) Open the WAV and feed it in (needs an absolute path).
    import os
    abs_path = os.path.abspath(path)
    file = await storage.StorageFile.get_file_from_path_async(abs_path)
    stream = await file.open_async(storage.FileAccessMode.READ)
    # SpeechRecognizer.recognize_async takes a SpeechRecognitionStream. WinRT
    # uses the file's stream; wrap via streams. The exact API:
    #   recognizer.recognize_async(speech_stream)
    try:
        t1 = time.perf_counter()
        result = await recognizer.recognize_async(stream)
        elapsed = time.perf_counter() - t1
        print(f"recognize: status={result.status} text={result.text!r}  ({elapsed:.2f}s)")
        print(f"  confidence: {result.raw_confidence if hasattr(result, 'raw_confidence') else 'n/a'}")
    except Exception as exc:
        print(f"[FAIL] recognize_async: {type(exc).__name__}: {exc}")


def main() -> int:
    path = sys.argv[1] if len(sys.argv) > 1 else "tests/sample.wav"
    asyncio.run(probe(path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
