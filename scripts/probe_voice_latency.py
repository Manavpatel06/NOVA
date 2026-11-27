"""Where does a spoken turn spend its time? (laptop -> hub, 2026-11-26)

Run from D:\\NOVA:  .venv\\Scripts\\python.exe scripts\\probe_voice_latency.py

Measures, through NOVA's own Egress, against the hub's talk endpoint:
  1. the network round trip (health), several times
  2. an instant command, without and with the spoken reply (hub TTS time + audio size)
  3. a question that needs Claude, without speech
  4. speech-to-text on the hub: NOVA's own spoken reply sent back as microphone audio
Nothing is said to anyone, no tool with side effects runs ("what time is it" only).
"""
from __future__ import annotations

import base64
import io
import json
import secrets
import statistics
import sys
import time
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nova.config import load_config  # noqa: E402
from nova.security.egress import Egress  # noqa: E402


class _NoAudit:
    def record(self, entry: dict) -> None:
        pass


def main() -> int:
    config = load_config()
    host = config.hub.host.strip().lower()
    egress = Egress(list(config.security.egress_allowlist) + [host], audit=_NoAudit(),
                    redact_terms=config.security.redact_terms)
    sid = "probe-" + secrets.token_hex(6)

    def talk(body: dict, timeout: float = 120.0) -> tuple[float, dict]:
        t = time.monotonic()
        out = egress.post_json(f"https://{host}/v1/talk", {"session": sid, "timing": True, **body}, headers={}, timeout=timeout)
        return time.monotonic() - t, out

    rtts = []
    for _ in range(5):
        t = time.monotonic()
        egress.fetch(f"https://{host}/v1/health", 15.0)
        rtts.append((time.monotonic() - t) * 1000)
    print(f"network round trip (health): median {statistics.median(rtts):.0f} ms, max {max(rtts):.0f} ms")

    secs, out = talk({"text": "Nova, what time is it", "speak": False})
    print(f"instant command, no voice:   {secs:.2f} s  -> {out.get('say')!r}")
    secs, out = talk({"text": "Nova, what time is it", "speak": True})
    audio = base64.b64decode(out.get("audio") or "")
    print(f"instant command, with voice: {secs:.2f} s  (reply audio {len(audio) / 1024:.0f} KB; hub: {out.get('ms')})")

    secs, out = talk({"text": "Nova, tell me one fun fact about Arizona in one short sentence", "speak": False})
    print(f"Claude question, no voice:   {secs:.2f} s  -> {str(out.get('say'))[:80]!r}")
    secs, out = talk({"text": "Nova, in three sentences, why is the sky blue?", "speak": True})
    print(f"Claude, 3 sentences + voice: {secs:.2f} s  until the first piece can play (hub: {out.get('ms')})")
    t = time.monotonic()
    for part in range(int(out.get("parts") or 0)):   # what a speaker does while the first piece plays
        egress.post_json(f"https://{host}/v1/talk/more", {"id": out["more"], "part": part}, headers={}, timeout=60)
    if out.get("parts"):
        print(f"  the other {out['parts']} piece(s) ready {time.monotonic() - t:.2f} s later")

    if audio:
        import numpy as np

        with wave.open(io.BytesIO(audio)) as w:
            rate = w.getframerate()
            pcm = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float32)
        n = int(len(pcm) * 16000 / rate)
        pcm16k = np.interp(np.linspace(0, len(pcm) - 1, n), np.arange(len(pcm)), pcm).astype("<i2").tobytes()
        seconds_of_audio = n / 16000
        secs, out = talk({"audio": base64.b64encode(pcm16k).decode("ascii"), "speak": False})
        print(f"speech-to-text on the hub:   {secs:.2f} s for {seconds_of_audio:.1f} s of audio  -> heard {out.get('heard')!r}"
              f"  (hub: {out.get('ms')})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
