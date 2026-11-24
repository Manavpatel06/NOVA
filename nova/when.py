"""Spoken times -> datetimes, for reminders and timers (2026-11-24).

    parse_when("in 10 minutes", now)       -> now + 10 min
    parse_when("at 5:30 pm", now)          -> today 17:30 (tomorrow if that's passed)
    parse_when("at 7", now)                -> the next 7:00 (am or pm, whichever is sooner)
    parse_when("tomorrow at 9am", now)     -> tomorrow 09:00
    parse_when("tomorrow", now)            -> tomorrow 09:00
    parse_when("tonight at 8", now)        -> today 20:00
    parse_duration("an hour and a half")   -> 5400

Local time on the machine NOVA runs on (Manav's laptop, in Tempe) -- no
time-zone database needed. Anything it doesn't understand is None, and the
caller asks "when?" rather than guessing (CLAUDE.md: confidence honesty).
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta

_UNITS = {
    "second": 1, "seconds": 1, "sec": 1, "secs": 1,
    "minute": 60, "minutes": 60, "min": 60, "mins": 60,
    "hour": 3600, "hours": 3600, "hr": 3600, "hrs": 3600,
    "day": 86400, "days": 86400,
}
_SMALL = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
    "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20,
    "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "ninety": 90, "a couple of": 2, "a couple": 2,
    "a few": 3, "few": 3,
}
_HOUR_WORDS = {k: v for k, v in _SMALL.items() if isinstance(v, int) and 1 <= v <= 12 and " " not in k
               and k not in ("a", "an", "few")}
MAX_AHEAD = timedelta(days=366)


def _number(text: str) -> float | None:
    text = text.strip()
    if re.fullmatch(r"\d+(\.\d+)?", text):
        return float(text)
    if text in _SMALL:
        return float(_SMALL[text])
    m = re.fullmatch(r"(twenty|thirty|forty|fifty)[ -](one|two|three|four|five|six|seven|eight|nine)", text)
    if m:
        return float(_SMALL[m.group(1)] + _SMALL[m.group(2)])
    return None


def parse_duration(text: str) -> int | None:
    """Seconds in "10 minutes", "an hour and a half", "half an hour",
    "1 hour 30 minutes", "90 secs". None if it isn't a duration."""
    t = " ".join((text or "").lower().replace(",", " ").split())
    t = re.sub(r"^(?:for|in)\s+", "", t)
    if not t:
        return None
    if t in ("half an hour", "a half hour", "half hour"):
        return 1800
    m = re.fullmatch(r"(.+?) and a half (\w+)", t)  # "an hour and a half" -> "an hour" + half of it
    half = False
    if m:
        t, half = f"{m.group(1)} {m.group(2)}", True
    elif t.endswith(" and a half"):
        t, half = t[: -len(" and a half")], True
    total = 0.0
    found = False
    last_unit = 0
    for part in re.split(r"\s+and\s+|(?<=[a-z])\s+(?=\d)", t):
        m = re.fullmatch(r"(.+?)\s*(seconds?|secs?|minutes?|mins?|hours?|hrs?|days?)", part.strip())
        if not m:
            return None
        n = _number(m.group(1))
        if n is None or n <= 0:
            return None
        last_unit = _UNITS[m.group(2)]
        total += n * last_unit
        found = True
    if not found:
        return None
    if half:
        total += last_unit / 2
    return int(total) if 0 < total <= MAX_AHEAD.total_seconds() else None


def _clock(text: str) -> tuple[int, int, str | None] | None:
    """(hour, minute, am/pm or None) from "5", "5:30", "5 30 pm", "17:30", "noon", "five"."""
    t = text.strip().replace(".", "")
    if t in ("noon", "midday"):
        return 12, 0, "pm"
    if t == "midnight":
        return 12, 0, "am"
    m = re.fullmatch(r"(\d{1,2})(?:[: ](\d{2}))?\s*(am|pm|a m|p m)?(?: o'?clock)?", t)
    if m:
        h, mi = int(m.group(1)), int(m.group(2) or 0)
        ap = (m.group(3) or "").replace(" ", "") or None
    else:
        m = re.fullmatch(r"([a-z]+)(?: o'?clock)?\s*(am|pm|a m|p m)?", t)
        if not m or m.group(1) not in _HOUR_WORDS:
            return None
        h, mi, ap = _HOUR_WORDS[m.group(1)], 0, (m.group(2) or "").replace(" ", "") or None
    if mi > 59 or h > 23 or (ap and not 1 <= h <= 12):
        return None
    return h, mi, ap


def _at(day: datetime, h: int, mi: int, ap: str | None) -> datetime:
    if ap == "pm" and h != 12:
        h += 12
    elif ap == "am" and h == 12:
        h = 0
    return day.replace(hour=h, minute=mi, second=0, microsecond=0)


def parse_when(text: str, now: datetime) -> datetime | None:
    """When a reminder is for, in `now`'s clock; None if unclear or past."""
    t = " ".join((text or "").lower().replace(",", " ").split())
    t = re.sub(r"^(?:remind me\s+)?", "", t)
    if not t:
        return None
    if t.startswith("in "):
        secs = parse_duration(t[3:])
        return now + timedelta(seconds=secs) if secs else None

    m = re.fullmatch(r"(tomorrow|today|tonight|this evening|this afternoon|this morning)"
                     r"(?:\s+(morning|evening|afternoon|night))?(?:\s+at\s+(.+))?", t)
    if m:
        word, part, clock_text = m.group(1), m.group(2), m.group(3)
    else:
        m = re.fullmatch(r"(?:at\s+)?(.+?)\s+(tomorrow|today|tonight)(?:\s+(morning|evening|afternoon|night))?", t)
        if m:
            clock_text, word, part = m.group(1), m.group(2), m.group(3)
    if m:
        day = now + timedelta(days=1) if word == "tomorrow" else now
        if clock_text is None:
            if word == "tomorrow" and part in (None, "morning"):
                return _at(day, 9, 0, "am")
            return None  # "tonight" alone: ask for a time
        c = _clock(clock_text)
        if c is None:
            return None
        h, mi, ap = c
        if ap is None and h <= 12:
            if word in ("tonight", "this evening", "this afternoon") or part in ("evening", "afternoon", "night"):
                ap = "pm"
            elif word == "this morning" or part == "morning":
                ap = "am"
            elif word == "tomorrow":
                ap = "am" if 7 <= h <= 11 else "pm"  # "tomorrow at 9" = 9 am, "at 3" = 3 pm
        if ap is not None or h > 12:
            when = _at(day, h, mi, ap)
        else:  # "today at 7": whichever 7 o'clock is next
            when = min((w for w in (_at(day, h % 12, mi, None), _at(day, h % 12 + 12, mi, None)) if w > now),
                       default=_at(day, h, mi, None))
        return when if now < when <= now + MAX_AHEAD else None

    m = re.fullmatch(r"(?:at\s+)?(.+)", t)
    c = _clock(m.group(1)) if m else None
    if c is None:
        return None
    h, mi, ap = c
    candidates = []
    if ap or h > 12 or h == 0:
        candidates.append(_at(now, h, mi, ap))
    elif 1 <= h <= 5:  # "at 3": 3 pm -- nobody means 3 am without saying so
        candidates.append(_at(now, h + 12, mi, None))
    else:  # "at 7": the next 7 o'clock, am or pm
        candidates += [_at(now, h % 12, mi, None), _at(now, h % 12 + 12, mi, None)]
    upcoming = sorted(c if c > now else c + timedelta(days=1) for c in candidates)
    return upcoming[0]


def say_when(when: datetime, now: datetime) -> str:
    """ "at 5:30 PM", "tomorrow at 9:00 AM", "on Friday at 9:00 AM", "in 10 minutes"."""
    delta = when - now
    if delta < timedelta(hours=1):
        mins = max(1, round(delta.total_seconds() / 60))
        return f"in {mins} minute{'s' if mins != 1 else ''}"
    clock = when.strftime("%I:%M %p").lstrip("0")
    if when.date() == now.date():
        return f"at {clock}"
    if when.date() == (now + timedelta(days=1)).date():
        return f"tomorrow at {clock}"
    if delta < timedelta(days=7):
        return f"on {when.strftime('%A')} at {clock}"
    return f"on {when.strftime('%B')} {when.day} at {clock}"
