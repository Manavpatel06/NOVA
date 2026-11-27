"""Does NOVA do what Manav asks? (2026-11-27) Run from D:\\NOVA:
    .venv\\Scripts\\python.exe scripts\\eval_commands.py [--only WORD]

Sends each sentence to NOVA on the hub as a DRY RUN: NOVA decides what to do, read-only skills really run, and
everything else is only written down -- nothing opens, nothing is sent, no reminder is set. Scores whether NOVA
picked the right skill (and the right key detail), and prints what it did otherwise.
"""
from __future__ import annotations

import json
import secrets
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nova.config import load_config  # noqa: E402
from nova.security.egress import Egress  # noqa: E402

ANSWER = "answer"   # should just answer: no action skill (read-only skills and web search are fine)
# (sentence, acceptable skills, {skill: (param, text that should be in it)})
CASES = [
    ("what time is it", {"time"}, {}),
    ("what's today's date", {"date"}, {}),
    ("what's the weather tomorrow", {"weather"}, {"weather": ("when", "tomorrow")}),
    ("is it going to rain in Tempe today", {"weather"}, {}),
    ("how much battery do I have", {"battery"}, {}),
    ("set the volume to 40", {"set_volume"}, {"set_volume": ("level", "40")}),
    ("turn it up a bit", {"volume_up"}, {}),
    ("mute", {"mute"}, {}),
    ("make the screen brighter", {"brightness_up", "set_brightness"}, {}),
    ("pause the music", {"media_play_pause", "media_stop"}, {}),
    ("next song", {"media_next"}, {}),
    ("open spotify", {"open_app", "switch_to_app"}, {"open_app": ("app", "spotify")}),
    ("open youtube in brave", {"open_website", "youtube"}, {"open_website": ("browser", "brave")}),
    ("search google for cheap flights to Ahmedabad", {"google_search"}, {"google_search": ("query", "ahmedabad")}),
    ("play lofi beats on youtube", {"youtube"}, {"youtube": ("query", "lofi")}),
    ("open visual studio code", {"open_app"}, {}),
    ("close chrome", {"close_app"}, {"close_app": ("app", "chrome")}),
    ("minimize this window", {"minimize_window"}, {}),
    ("show me the desktop", {"show_desktop"}, {}),
    ("take a screenshot", {"screenshot"}, {}),
    ("lock my laptop", {"lock_computer"}, {}),
    ("put the laptop to sleep", {"sleep_computer"}, {}),
    ("turn off bluetooth", {"set_radio"}, {"set_radio": ("radio", "bluetooth")}),
    ("open wifi settings", {"open_settings"}, {}),
    ("open my resume", {"open_file"}, {"open_file": ("name", "resume")}),
    ("open the CSE 576 folder", {"open_file"}, {"open_file": ("name", "576")}),
    ("remind me to call mom at 7 pm", {"remind_me"}, {"remind_me": ("text", "mom")}),
    ("set a timer for ten minutes", {"set_timer"}, {}),
    ("what reminders do I have", {"list_reminders"}, {}),
    ("call me at 6 tomorrow to wake me up", {"remind_me"}, {"remind_me": ("call", "yes")}),
    ("remember that my favourite drink is cold coffee", {"remember"}, {"remember": ("fact", "coffee")}),
    ("what do you remember about my drinks", {"recall"}, {}),
    ("how's the hub doing", {"hub_status"}, {}),
    ("restart yourself", {"restart_nova"}, {}),
    ("learn the word HydraWav3", {"learn_word"}, {"learn_word": ("word", "hydrawav3")}),
    ("build me a simple portfolio website", {"build_project"}, {}),
    ("what can you do", {"capabilities", ANSWER}, {}),
    ("what's due on canvas this week", {"canvas_get_my_upcoming_assignments", "canvas_get_my_todo_items",
                                        "canvas_list_assignments"}, {}),
    ("check my school email for anything from my professor", {"google_search_gmail_messages"}, {}),
    ("what's on my calendar tomorrow", {"google_get_events"}, {}),
    ("tell me a joke", {ANSWER}, {}),
    ("who wrote pride and prejudice", {ANSWER}, {"say": ("", "austen")}),
    ("what's 18 percent of 240", {ANSWER}, {"say": ("", "43.2")}),
    ("what's the capital of Australia", {ANSWER}, {"say": ("", "canberra")}),
    ("what's 17 times 23", {ANSWER}, {"say": ("", "391")}),
    ("how many days are there in a leap year", {ANSWER}, {"say": ("", "366")}),
    ("explain how vaccines work in two sentences", {ANSWER}, {}),
    ("I think the moon is bigger than the earth, right", {ANSWER}, {"say": ("", "smaller")}),
    ("shut down the laptop", {"shutdown_computer"}, {}),
]


class _NoAudit:
    def record(self, entry):
        pass


def main() -> int:
    only = sys.argv[sys.argv.index("--only") + 1].lower() if "--only" in sys.argv else ""
    config = load_config()
    host = config.hub.host.strip().lower()
    eg = Egress(list(config.security.egress_allowlist) + [host], audit=_NoAudit(),
                redact_terms=config.security.redact_terms, keep_alive_hosts=[host])
    ok = 0
    rows = []
    cases = [c for c in CASES if only in c[0].lower()]
    for text, want, details in cases:
        out = eg.post_json(f"https://{host}/v1/talk", {"session": "eval-" + secrets.token_hex(6), "text": text,
                                                       "speak": False, "dry_run": True}, headers={}, timeout=120)
        calls = [c for c in out.get("dry_run") or [] if c.get("skill") not in ("web_search",)]
        actions = [c for c in calls if c["skill"] not in {"time", "date", "weather", "battery", "recall",
                                                          "capabilities", "get_volume", "get_brightness"}]
        names = [c["skill"] for c in calls]
        if want == {ANSWER}:
            good = not actions
        else:
            good = any(n in want for n in names) or (ANSWER in want and not actions)
            for c in calls:
                if c["skill"] in details:
                    p, needle = details[c["skill"]]
                    if needle not in str(c.get("params", {}).get(p, "")).lower():
                        good = False
        if "say" in details and details["say"][1] not in str(out.get("say", "")).lower().replace(",", ""):
            good = False   # the answer itself must be right
        ok += good
        rows.append((good, text, names, out.get("say", "")))
        print(f"{'ok  ' if good else 'MISS'} {text!r:55} -> {names or '(answered)'}  {str(out.get('say'))[:70]!r}",
              flush=True)
        time.sleep(0.3)
    print(f"\n{ok}/{len(cases)} right ({100 * ok / max(1, len(cases)):.0f} %)")
    Path("data").mkdir(exist_ok=True)
    Path("data/eval_commands_last.json").write_text(json.dumps(rows, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
