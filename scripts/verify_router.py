"""Calibration + verification script for T0.6c (the router). Not part of the
test suite. Prints the full score table the router's docstring cites, then
exercises route() end to end: exact examples, paraphrases, ambiguous/gibberish
input, Untrusted rejection, and open_app parameter extraction.

Run: D:\\NOVA\\.venv\\Scripts\\python.exe scripts\\verify_router.py
"""

from __future__ import annotations

import nova.skills  # triggers discovery
from nova.brain.router import Router, _similarity
from nova.config import load_config
from nova.security.untrusted import Untrusted
from nova.skills.registry import all_skills


def section(title: str) -> None:
    print(f"\n=== {title} ===")


LABELLED = {
    "time (literal examples)": [
        "what time is it",
        "what's the time",
        "tell me the time",
        "current time",
    ],
    "time (paraphrases, never seen verbatim)": [
        "do you know what time it is",
        "can you tell me the time please",
        "what's the current time right now",
    ],
    "open_app (literal examples)": [
        "open notepad",
        "launch calculator",
        "open file explorer",
        "open task manager",
        "open control panel",
    ],
    "open_app (paraphrases, never seen verbatim)": [
        "could you open notepad for me",
        "please launch the calculator",
        "start notepad",
        "pull up task manager",
        "open the control panel please",
    ],
    "off-topic / gibberish": [
        "what's the weather today",
        "play some music",
        "asdkfj qwoeiru xyzzy",
        "how are you doing",
        "set a timer for five minutes",
        "send an email to john about the meeting",
    ],
}


def main() -> None:
    skills = all_skills()
    print(f"registry: {sorted(skills)}")

    section("score table (best-matching example per skill)")
    best_clean = 1.0
    worst_clean = 1.0
    best_offtopic = 0.0
    for category, phrases in LABELLED.items():
        print(f"\n-- {category} --")
        for text in phrases:
            row = []
            for name, spec in skills.items():
                best = max((_similarity(text, ex) for ex in spec.examples), default=0.0)
                row.append(f"{name}={best:.3f}")
            print(f"  {text!r:60s} {'  '.join(row)}")
            top = max(max((_similarity(text, ex) for ex in spec.examples), default=0.0) for spec in skills.values())
            if "gibberish" in category or "off-topic" in category:
                best_offtopic = max(best_offtopic, top)
            else:
                worst_clean = min(worst_clean, top)

    print(f"\nworst clean-match top score: {worst_clean:.3f}")
    print(f"best off-topic/gibberish top score: {best_offtopic:.3f}")
    print(f"gap available for confidence_floor: ({best_offtopic:.3f}, {worst_clean:.3f})")

    cfg = load_config()
    print(f"\nconfig.brain = {cfg.brain}")
    router = Router(confidence_floor=cfg.brain.confidence_floor, ambiguity_margin=cfg.brain.ambiguity_margin)

    section("route(): clean matches route with no clarification")
    for text in ["what time is it", "do you know what time it is", "open notepad", "please launch the calculator"]:
        m = router.route(text)
        print(f"{text!r:45s} -> spec={m.spec.name if m.spec else None!r:12s} params={m.params} conf={m.confidence:.3f} clarify={m.clarification!r}")
        assert m.spec is not None and m.clarification is None, f"FAIL: expected a clean route for {text!r}, got {m}"

    section("route(): off-topic / gibberish asks instead of guessing")
    for text in ["what's the weather today", "play some music", "asdkfj qwoeiru xyzzy", "send an email to john"]:
        m = router.route(text)
        print(f"{text!r:45s} -> spec={m.spec!r} conf={m.confidence:.3f} clarify={m.clarification!r}")
        assert m.spec is None and m.clarification is not None, f"FAIL: expected a clarification for {text!r}, got {m}"

    section("route(): open_app parameter extraction")
    for text, expected_app in [
        ("open notepad", "notepad"),
        ("launch calculator", "calculator"),
        ("could you open notepad for me", "notepad"),
        ("open the control panel please", "control panel"),
        ("open task manager", "task manager"),
    ]:
        m = router.route(text)
        print(f"{text!r:35s} -> params={m.params}")
        assert m.spec is not None and m.spec.name == "open_app", f"FAIL: {text!r} did not route to open_app: {m}"
        assert m.params.get("app") == expected_app, f"FAIL: {text!r} extracted app={m.params.get('app')!r}, expected {expected_app!r}"

    section("route(): can't extract a param confidently -> clarify, not guess")
    for text in ["open"]:
        m = router.route(text)
        print(f"{text!r:35s} -> spec={m.spec!r} clarify={m.clarification!r}")
        # "open" alone may fail on confidence floor OR on param extraction --
        # either way it must never silently invent an app name.
        assert m.spec is None or not m.params, f"FAIL: {text!r} silently guessed a param: {m}"

    section("route(): Untrusted content is refused at the boundary")
    u = Untrusted("open notepad", source="mail.body")
    try:
        router.route(u)  # type: ignore[arg-type]
    except TypeError as exc:
        print(f"TypeError raised, as required: {exc}")
    else:
        raise SystemExit("FAIL: router accepted Untrusted content")

    section("route(): non-str raises TypeError too")
    try:
        router.route(12345)  # type: ignore[arg-type]
    except TypeError as exc:
        print(f"TypeError raised: {exc}")
    else:
        raise SystemExit("FAIL: router accepted a non-str")

    print("\nALL CHECKS PASSED")


if __name__ == "__main__":
    main()
