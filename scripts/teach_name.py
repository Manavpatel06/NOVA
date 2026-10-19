#!/usr/bin/env python
"""Teach NOVA how the speech model hears YOU say "Nova".

Whisper sometimes writes the spoken name as a different word for a given
voice and microphone ("Noah", "Nava", ...). NOVA only acts on sentences
that START with its name, so a mismatch means it never responds.

This records you saying a few "Nova, ..." sentences through exactly the
same microphone path and speech model NOVA uses, shows what was heard, and
saves the spellings that came up at least twice to data\\wake_names.json.
Common words ("oh", "no", "so", "that", ...) are never accepted as the
name, because NOVA would then react to ordinary conversation.

Close NOVA first (it holds the microphone), then run:

    D:\\NOVA\\.venv\\Scripts\\python.exe scripts\\teach_name.py

Nothing leaves this machine; the recordings are not saved.
"""
from __future__ import annotations

import json
import logging
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np

from nova import WAKE_NAMES_PATH
from nova.audio.capture import Capture
from nova.audio.utterance import UtteranceSegmenter
from nova.audio.vad import Vad
from nova.brain.address import UNSAFE_NAMES, first_name_word
from nova.config import load_config
from nova.stt.transcriber import Transcriber

PROMPTS = [
    "Nova, what time is it?",
    "Nova, open Notepad.",
    "Hey Nova, what's the date today?",
    "Nova, what can you do?",
    "Nova, open Spotify.",
    "Hey Nova, what time is it?",
]
LISTEN_S = 8.0
SILENT_RMS = 3e-4  # hint threshold only; a good mic in a quiet room can read lower

_log = logging.getLogger("nova.teach_name")


def _setup_logging() -> None:
    from nova import DATA_DIR

    h = logging.FileHandler(DATA_DIR / "nova.log", encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)-8s %(name)s: %(message)s", "%Y-%m-%dT%H:%M:%S%z"))
    root = logging.getLogger()
    root.addHandler(h)
    root.setLevel(logging.INFO)


def _room_level(cap: Capture, seconds: float = 2.0) -> float:
    frames = [cap.read(timeout=2.0) for _ in range(int(seconds * 1000 / 30))]
    x = np.concatenate(frames)
    return float(np.sqrt(np.mean(x * x)))


def choose_names(first_words: list[str | None], base: list[str], min_count: int = 2) -> list[str]:
    """Spellings heard at least `min_count` times that are safe to accept."""
    counts = Counter(w for w in first_words if w)
    base_l = {b.lower() for b in base}
    return sorted(
        w for w, c in counts.items()
        if c >= min_count and w.isalpha() and w not in UNSAFE_NAMES and w not in base_l
    )


def _record_one(cap: Capture, seg: UtteranceSegmenter) -> np.ndarray | None:
    seg.reset()
    deadline = time.monotonic() + LISTEN_S
    while time.monotonic() < deadline:
        utt = seg.process(cap.read(timeout=2.0))
        if utt is not None:
            return utt
    return None


def main() -> int:
    _setup_logging()
    cfg = load_config()
    names = list(cfg.wake.names) or ["nova"]
    print("Loading the speech model (same one NOVA uses)...")
    stt = Transcriber(
        language=cfg.stt.language,
        hotwords=", ".join(n.capitalize() for n in names),
    )
    stt.load()
    vad = Vad()
    vad.load()
    seg = UtteranceSegmenter(vad, silence_ms=800, max_ms=10000, min_speech_ms=300)
    cap = Capture()
    print(f"Microphone: {cap.profile.name}\n")
    print("For each line, wait for SPEAK, then say it naturally, like you would to NOVA.\n")

    heard: list[str | None] = []
    cap.start()
    try:
        level = _room_level(cap)
        _log.info("teach_name: mic level check rms=%.6f", level)
        if level < SILENT_RMS:
            # Only a hint: a good mic in a quiet room can read this low too
            # (Manav's did on 2026-10-14 and still heard him 6/6), so never stop.
            print("   (very quiet so far -- if nothing gets heard below, check the mic-mute key)")
            _log.info("teach_name: room very quiet (rms=%.6f); continuing", level)
        for i, prompt in enumerate(PROMPTS, 1):
            print(f'[{i}/{len(PROMPTS)}]  "{prompt}"')
            time.sleep(1.0)
            print("   SPEAK NOW", flush=True)
            utt = _record_one(cap, seg)
            if utt is None:
                print("   (didn't hear anything -- skipped)\n")
                heard.append(None)
                _log.info("teach_name: prompt %d -- no speech detected", i)
                continue
            text = stt.transcribe(utt) or ""
            word = first_name_word(text)
            heard.append(word)
            _log.info("teach_name: prompt %d heard name-position word %r", i, word)
            print(f"   heard: {text!r}   -> name position: {word!r}\n")
    finally:
        cap.stop()
        stt.unload()

    already = sum(1 for w in heard if w in {n.lower() for n in names} or w == "novah")
    learned = choose_names(heard, names)
    _log.info("teach_name: done -- correct %d/%d, learned %s", already, len(PROMPTS), learned)
    print("=" * 70)
    print(f'"Nova" recognised correctly {already}/{len(PROMPTS)} times.')
    if learned:
        WAKE_NAMES_PATH.parent.mkdir(parents=True, exist_ok=True)
        WAKE_NAMES_PATH.write_text(json.dumps({"names": learned}, indent=2), encoding="utf-8")
        print(f"NOVA will now also answer to: {', '.join(learned)}  (saved to {WAKE_NAMES_PATH})")
    elif already >= len(PROMPTS) // 2:
        print("No extra spellings needed -- the model already hears your 'Nova'.")
    else:
        unsafe = sorted({w for w in heard if w and w in UNSAFE_NAMES})
        print("No safe spelling came up twice." + (f" (Ignored common words: {', '.join(unsafe)})" if unsafe else ""))
        print("Try again a little closer to the mic, saying 'Nova' clearly as its own word.")
    print("Restart NOVA, then tell Claude 'check logs'.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
