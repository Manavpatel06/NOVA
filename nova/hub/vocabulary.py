"""Words NOVA should hear right (2026-11-27, "make NOVA accurate").

Whisper spells unusual words its own way: "HydraWav3" came out "Hydra Vav3",
"Tempe" "intent", "ARC" "Ark". Given a short list of the words Manav uses,
it gets them right. Measured on 60 test commands (6 voices, room noise):

    before (beam 1, hotwords "Nova Manav")   word errors 7.1 %, key words heard 91.9 %
    after  (beam 5, this list as a prompt)   word errors 2.8 %, key words heard 97.5 %

and no extra "Nova" heard in 10 sentences not meant for NOVA or 10 noise clips.

The list is: NOVA's names, Manav's own words (data/vocabulary.txt, added by
voice with "Nova, learn the word ..." or by hand), his app names, his
connected-app account labels, and his Canvas course codes (refreshed twice a
day when Canvas is connected). Never more than MAX_CHARS of prompt.
"""
from __future__ import annotations

import re
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterable

from nova.log import get_logger

_log = get_logger("hub.vocabulary")

MAX_CHARS = 700          # Whisper's prompt is short (~224 tokens); keep well inside it
MAX_WORD = 40
COURSE_RE = re.compile(r"\b([A-Z]{2,4}) ?(\d{3}[A-Z]?)\b")
COURSES_EVERY_S = 12 * 3600
WORD_RE = re.compile(r"[\w][\w .&'+-]{0,39}", re.UNICODE)
BASE = ("Nova", "Manav", "Tempe", "ASU", "Canvas", "Gmail")


def clean(word: str) -> str | None:
    word = " ".join(str(word or "").split()).strip(" .,;:")
    if not word or len(word) > MAX_WORD or not WORD_RE.fullmatch(word):
        return None
    return word


class Vocabulary:
    def __init__(self, path: Path, *, names: Iterable[str] = ("nova",), apps: Iterable[str] = (),
                 connectors: Any = None, clock: Callable[[], float] = time.monotonic) -> None:
        self.path = Path(path)
        self._names = [n.capitalize() for n in names]
        self._apps = [a for a in (clean(x.replace("_", " ").title()) for x in apps) if a]
        self.connectors = connectors
        self._clock = clock
        self._lock = threading.Lock()
        self._courses: list[str] = []
        self._courses_at = -COURSES_EVERY_S
        self._refreshing = False

    # --- Manav's own words ---------------------------------------------------------------

    def words(self) -> list[str]:
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        return [w for w in (clean(x) for x in lines if not x.startswith("#")) if w]

    def learn(self, word: str) -> str | None:
        w = clean(word)
        if w is None:
            return None
        with self._lock:
            have = self.words()
            if w.lower() not in {x.lower() for x in have}:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self.path.write_text("\n".join([*have, w]) + "\n", encoding="utf-8")
        return w

    def forget(self, word: str) -> bool:
        w = (clean(word) or "").lower()
        with self._lock:
            have = self.words()
            keep = [x for x in have if x.lower() != w]
            if len(keep) == len(have):
                return False
            self.path.write_text("\n".join(keep) + ("\n" if keep else ""), encoding="utf-8")
        return True

    # --- Canvas course codes ------------------------------------------------------------

    def _maybe_refresh_courses(self) -> None:
        if self.connectors is None or self._refreshing or self._clock() - self._courses_at < COURSES_EVERY_S:
            return
        self._refreshing = True

        def run() -> None:
            try:
                ready = {s["name"] for s in self.connectors.status() if s.get("state") == "ready"}
                if "canvas" in ready:
                    text, err = self.connectors.call("canvas", "list_courses", {})
                    if not err:
                        found = sorted({f"{a} {b}" for a, b in COURSE_RE.findall(text or "")})[:20]
                        self._courses = found
                        _log.info("Vocabulary: %d course codes from Canvas", len(found))
                self._courses_at = self._clock()
            except Exception as exc:  # noqa: BLE001 -- try again later
                _log.warning("Couldn't read course codes (%s)", type(exc).__name__)
                self._courses_at = self._clock() - COURSES_EVERY_S + 600
            finally:
                self._refreshing = False

        threading.Thread(target=run, name="vocabulary", daemon=True).start()

    # --- the prompt ---------------------------------------------------------------------

    def terms(self) -> list[str]:
        self._maybe_refresh_courses()
        accounts: list[str] = []
        if self.connectors is not None:
            try:
                for e in getattr(self.connectors, "_entries", {}).values():
                    accounts += [a.capitalize() for a in (e.get("accounts") or {})]
            except Exception:  # noqa: BLE001
                pass
        seen: set[str] = set()
        out: list[str] = []
        # his own words first: they matter most and must survive the length cap
        for w in [*self._names, *BASE[1:], *self.words(), *self._courses, *self._apps, *accounts]:
            if w and w.lower() not in seen:
                seen.add(w.lower())
                out.append(w)
        return out

    def prompt(self) -> str:
        words: list[str] = []
        size = len("Words: .")
        for w in self.terms():
            if size + len(w) + 2 > MAX_CHARS:
                break
            words.append(w)
            size += len(w) + 2
        return f"Words: {', '.join(words)}." if words else ""
