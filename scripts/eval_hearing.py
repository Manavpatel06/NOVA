"""How well does NOVA hear? (ADR 0034) Run on the hub:  ~/NOVA/.venv/bin/python scripts/eval_hearing.py

Speaks 30 NOVA-style commands in several voices with room noise (NOVA's own voice model), hears them back with
the hub's real settings and word list, and reports word errors, key words heard, and whether chatter or noise
ever sounds like "Nova". Nothing leaves the hub.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nova import DATA_DIR  # noqa: E402
from nova.brain.address import strip_address  # noqa: E402
from nova.config import load_config  # noqa: E402
from nova.hub.vocabulary import Vocabulary  # noqa: E402
from nova.hub.voice import HubVoice  # noqa: E402

PHRASES = [
    ("Nova, open the CSE 576 folder.", ["nova", "cse 576"]), ("Nova, what's due on Canvas this week?", ["nova", "canvas"]),
    ("Nova, remind me to call Mom at 7 PM.", ["nova", "remind", "7"]), ("Nova, play lofi on Spotify.", ["nova", "spotify"]),
    ("Nova, set the volume to 40.", ["nova", "volume", "40"]), ("Nova, what's the weather in Tempe tomorrow?", ["nova", "tempe"]),
    ("Nova, how's the hub doing?", ["nova", "hub"]), ("Nova, check my personal Gmail.", ["nova", "gmail"]),
    ("Nova, open my resume.", ["nova", "resume"]), ("Nova, turn off Bluetooth.", ["nova", "bluetooth"]),
    ("Nova, take a screenshot.", ["nova", "screenshot"]), ("Nova, lock my laptop.", ["nova", "lock"]),
    ("Nova, open Visual Studio Code.", ["nova", "visual studio code"]), ("Nova, next song.", ["nova", "next"]),
    ("Hey Nova, it's Manav. How are you doing?", ["nova", "manav"]), ("Nova, confirm.", ["nova", "confirm"]),
    ("Nova, what's on my calendar for Thursday?", ["nova", "calendar"]), ("Nova, mute.", ["nova", "mute"]),
    ("Nova, open YouTube in Brave.", ["nova", "youtube", "brave"]), ("Nova, build me a portfolio website.", ["nova", "website"]),
]
CHAT = ["I'll be home around seven, can you pick up some milk?", "No, the meeting moved to Thursday afternoon.",
        "Did you see the game last night?", "My professor said the quiz is next week.", "Hold on, my phone is ringing."]
VOICES = ["am_michael", "af_heart", "hm_omega", "bm_george"]


def norm(t):
    t = re.sub(r"[^a-z0-9' ]+", " ", t.lower().replace("p.m.", "pm").replace("’", "'"))
    return t.split()


def wer(r, h):
    r, h = norm(r), norm(h)
    d = np.zeros((len(r) + 1, len(h) + 1), int)
    d[:, 0], d[0, :] = range(len(r) + 1), range(len(h) + 1)
    for i in range(1, len(r) + 1):
        for j in range(1, len(h) + 1):
            d[i, j] = min(d[i - 1, j] + 1, d[i, j - 1] + 1, d[i - 1, j - 1] + (r[i - 1] != h[j - 1]))
    return d[-1, -1], len(r)


def main() -> int:
    config = load_config()
    vocab = Vocabulary(DATA_DIR / "vocabulary.txt", names=config.wake.names, apps=list(config.security.allowed_apps))
    voice = HubVoice(prompt=vocab.prompt)
    tts = voice._load_tts()
    rng = np.random.default_rng(1)

    def clip(text, v):
        pcm, rate = tts.create(text, voice=v, lang="en-us")
        n = int(len(pcm) * 16000 / rate)
        a = np.interp(np.linspace(0, len(pcm) - 1, n), np.arange(len(pcm)), pcm) * 0.4
        a = np.concatenate([np.zeros(4000), a, np.zeros(6000)]) + rng.normal(0, 0.01, n + 10000)
        return (np.clip(a, -1, 1) * 32767).astype("<i2").tobytes()

    errs = words = keys = hits = 0
    misses = []
    for i, (text, ks) in enumerate(PHRASES):
        heard = voice.transcribe(clip(text, VOICES[i % len(VOICES)]))
        e, n = wer(text, heard)
        errs, words = errs + e, words + n
        flat = " ".join(norm(heard))
        for k in ks:
            keys += 1
            if " ".join(norm(k)) in flat:
                hits += 1
            else:
                misses.append(f"{k!r} in {heard!r}")
    false = [h for h in (voice.transcribe(clip(t, VOICES[i % 4])) for i, t in enumerate(CHAT))
             if strip_address(h, config.wake.names) is not None]
    print(f"word errors {100 * errs / words:.1f} %   key words heard {100 * hits / keys:.1f} %   "
          f"chatter heard as 'Nova': {len(false)}/{len(CHAT)}")
    for m in misses:
        print("   missed", m)
    print(f"word list: {vocab.prompt()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
