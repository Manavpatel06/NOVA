"""Reminders and timers (2026-11-24): spoken times, the hub's store and
scheduler, Bark alerts, the ack page, and the laptop skills."""
from __future__ import annotations

import dataclasses
import json
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta

import pytest

import nova.skills  # noqa: F401 -- registers the skills
from nova.brain.router import Router
from nova.config import Config, HubConfig
from nova.hub.listener import OWNER_HEADER, make_server
from nova.hub.notify import PUSH_URL, Bark
from nova.hub.reminders import MAX_SENDS, RESEND_S, ReminderError, ReminderStore
from nova.hub.service import HubError, HubService
from nova.security.audit import AuditLog
from nova.security.egress import EgressError
from nova.security.tiers import ArmedBy
from nova.skills import reminders as rem_skills
from nova.skills.registry import SkillCtx, get_skill
from nova.when import parse_duration, parse_when, say_when

NOW = datetime(2026, 11, 24, 15, 10)  # a Tuesday, 3:10 pm


# --- spoken times --------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("in 10 minutes", NOW + timedelta(minutes=10)),
    ("in an hour and a half", NOW + timedelta(minutes=90)),
    ("in half an hour", NOW + timedelta(minutes=30)),
    ("in twenty five minutes", NOW + timedelta(minutes=25)),
    ("in 2 hours 30 minutes", NOW + timedelta(minutes=150)),
    ("at 5:30 pm", datetime(2026, 11, 24, 17, 30)),
    ("at 17:45", datetime(2026, 11, 24, 17, 45)),
    ("at 7", datetime(2026, 11, 24, 19, 0)),          # the next 7 o'clock
    ("at 11", datetime(2026, 11, 24, 23, 0)),
    ("at 3", datetime(2026, 11, 25, 15, 0)),          # bare 1-5 = pm; 3 pm has passed
    ("at 3 am", datetime(2026, 11, 25, 3, 0)),
    ("at five", datetime(2026, 11, 24, 17, 0)),
    ("at noon", datetime(2026, 11, 25, 12, 0)),
    ("tomorrow", datetime(2026, 11, 25, 9, 0)),
    ("tomorrow morning", datetime(2026, 11, 25, 9, 0)),
    ("tomorrow at 9", datetime(2026, 11, 25, 9, 0)),
    ("tomorrow at 3", datetime(2026, 11, 25, 15, 0)),
    ("tomorrow evening at 7", datetime(2026, 11, 25, 19, 0)),
    ("tonight at 8", datetime(2026, 11, 24, 20, 0)),
    ("8 tonight", datetime(2026, 11, 24, 20, 0)),
    ("5 pm tomorrow", datetime(2026, 11, 25, 17, 0)),
])
def test_spoken_times(text, expected):
    assert parse_when(text, NOW) == expected


@pytest.mark.parametrize("text", ["", "tonight", "next week", "today at 2", "at 25", "at 7:75", "in banana minutes",
                                  "soon", "in 400 days"])
def test_unclear_or_past_times_are_none_so_nova_asks(text):
    assert parse_when(text, NOW) is None


@pytest.mark.parametrize("text,secs", [("10 minutes", 600), ("an hour and a half", 5400), ("90 secs", 90),
                                       ("1 hour and 15 minutes", 4500), ("5 minute", 300), ("for 2 hours", 7200)])
def test_durations(text, secs):
    assert parse_duration(text) == secs


@pytest.mark.parametrize("text", ["banana", "", "0 minutes", "minutes"])
def test_non_durations(text):
    assert parse_duration(text) is None


def test_say_when_is_written_for_the_ear():
    assert say_when(NOW + timedelta(minutes=10), NOW) == "in 10 minutes"
    assert say_when(datetime(2026, 11, 24, 17, 30), NOW) == "at 5:30 PM"
    assert say_when(datetime(2026, 11, 25, 9, 0), NOW) == "tomorrow at 9:00 AM"
    assert say_when(datetime(2026, 11, 27, 9, 0), NOW) == "on Friday at 9:00 AM"


# --- the hub's store and scheduler ---------------------------------------------

class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def _store(tmp_path, clock):
    return ReminderStore(tmp_path / "reminders.json", clock=clock)


@pytest.mark.parametrize("text,due", [("", 1_000_100), ("x", "soon"), ("x", True), ("x", 1_000_000 - 3600),
                                      ("x", 1_000_000 + 400 * 86400)])
def test_bad_reminders_are_refused(tmp_path, text, due):
    with pytest.raises(ReminderError):
        _store(tmp_path, Clock()).add(text, due)


def test_a_normal_reminder_fires_once_when_due(tmp_path):
    clock = Clock()
    store = _store(tmp_path, clock)
    store.add("call mom", clock.t + 300)
    sent = []
    assert store.tick(lambda r, late: sent.append(r["text"]) or True) == 0
    clock.t += 300
    assert store.tick(lambda r, late: sent.append(r["text"]) or True) == 1
    clock.t += 600
    assert store.tick(lambda r, late: sent.append(r["text"]) or True) == 0
    assert sent == ["call mom"] and store.pending() == []


def test_an_urgent_reminder_repeats_until_acknowledged(tmp_path):
    clock = Clock()
    store = _store(tmp_path, clock)
    r = store.add("leave for the airport", clock.t, urgent=True)
    sends = []
    send = lambda rem, late: sends.append(clock.t) or True  # noqa: E731
    store.tick(send)
    clock.t += RESEND_S - 1
    store.tick(send)          # too soon
    clock.t += 1
    store.tick(send)          # a minute later: again
    assert len(sends) == 2
    assert store.ack(r["id"], "wrong-token") is None
    assert store.ack(r["id"], r["token"])["acked"]
    clock.t += RESEND_S
    store.tick(send)
    assert len(sends) == 2 and store.pending() == []


def test_urgent_stops_after_max_sends(tmp_path):
    clock = Clock()
    store = _store(tmp_path, clock)
    store.add("x", clock.t, urgent=True)
    n = 0
    for _ in range(MAX_SENDS + 5):
        n += store.tick(lambda r, late: True)
        clock.t += RESEND_S
    assert n == MAX_SENDS and store.pending() == []


def test_a_failed_send_is_retried_and_a_late_one_says_so(tmp_path):
    clock = Clock()
    store = _store(tmp_path, clock)
    store.add("x", clock.t + 10)
    clock.t += 13 * 3600     # the hub was off past the reminder
    lates = []
    assert store.tick(lambda r, late: False) == 0
    assert store.tick(lambda r, late: lates.append(late) or True) == 1
    assert lates == [True]


def test_cancel_all(tmp_path):
    clock = Clock()
    store = _store(tmp_path, clock)
    store.add("a", clock.t + 60)
    store.add("b", clock.t + 120)
    assert store.cancel("all") == 2 and store.pending() == []
    clock.t += 200
    assert store.tick(lambda r, late: True) == 0


def test_done_reminders_are_dropped_after_a_week(tmp_path):
    clock = Clock()
    store = _store(tmp_path, clock)
    store.add("a", clock.t)
    store.tick(lambda r, late: True)
    clock.t += 8 * 86400
    store.tick(lambda r, late: True)
    assert json.loads((tmp_path / "reminders.json").read_text()) == []


# --- Bark ------------------------------------------------------------------------

class _Egress:
    def __init__(self, exc=None):
        self.calls, self.exc = [], exc

    def post_json(self, url, body, *, headers, timeout=20.0, method="POST"):
        self.calls.append((url, body))
        if self.exc:
            raise self.exc
        return {"code": 200}


def _bark(tmp_path, egress, show_text=False, key="abcDEF1234567890"):
    if key:
        (tmp_path / "bark_key").write_text(key + "\n")
    return Bark(egress, key_path=tmp_path / "bark_key", hub_name=lambda: "nova-hub.tail1.ts.net", show_text=show_text)


def test_by_default_the_reminder_words_never_leave_the_hub(tmp_path):
    eg = _Egress()
    r = {"id": "0a1b2c3d", "token": "tok123456789", "text": "pick up the prescription", "urgent": False}
    assert _bark(tmp_path, eg).send(r)
    url, body = eg.calls[0]
    assert url == PUSH_URL and "prescription" not in json.dumps(body)
    assert body["level"] == "timeSensitive" and "call" not in body
    assert body["url"] == "https://nova-hub.tail1.ts.net/v1/ack/0a1b2c3d?t=tok123456789"


def test_alert_text_on_puts_the_words_in_the_notification(tmp_path):
    eg = _Egress()
    _bark(tmp_path, eg, show_text=True).send({"id": "a", "token": "b", "text": "call mom", "urgent": False})
    assert eg.calls[0][1]["body"] == "call mom"


def test_urgent_is_a_critical_ringing_alert(tmp_path):
    eg = _Egress()
    _bark(tmp_path, eg).send({"id": "a", "token": "b", "text": "x", "urgent": True})
    body = eg.calls[0][1]
    assert body["level"] == "critical" and body["call"] == "1"


def test_no_key_or_a_failed_push_means_try_again(tmp_path):
    assert not _bark(tmp_path, _Egress(), key="").send({"id": "a", "token": "b", "text": "x"})
    assert not _bark(tmp_path, _Egress(exc=EgressError("down"))).send({"id": "a", "token": "b", "text": "x"})


# --- hub service + listener ---------------------------------------------------------

class _Model:
    model = "m"

    def installed(self):
        return ["m"]

    def loaded(self):
        return ["m"]


def _service(tmp_path, clock=None, notifier=None):
    (tmp_path / "hub_owner").write_text("manav@example.com\n")
    return HubService(model=_Model(), data_dir=tmp_path, clock=clock or Clock(), notifier=notifier)


def test_ack_page_escapes_the_reminder_text(tmp_path):
    svc = _service(tmp_path)
    out = svc.add_reminder({"text": "<img src=x onerror=alert(1)>", "due": svc.reminders._clock() + 60})
    r = svc.reminders.get(out["id"])
    page = svc.ack_page(r["id"], r["token"])
    assert "<img" not in page and "&lt;img" in page and "Got it" in page
    assert "Got it" in svc.ack_page(r["id"], r["token"])        # tapping twice is fine
    assert "Not found" in svc.ack_page(r["id"], "wrong-token")


def test_add_reminder_errors_are_400s(tmp_path):
    with pytest.raises(HubError):
        _service(tmp_path).add_reminder({"text": "x", "due": "later"})


@pytest.fixture
def hub(tmp_path):
    svc = _service(tmp_path, clock=__import__("time").time)
    server = make_server(svc, 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server.server_address, svc
    server.shutdown()
    server.server_close()


def _call(addr, method, path, body=None, login=None, raw=False):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(f"http://{addr[0]}:{addr[1]}{path}", data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if login:
        req.add_header(OWNER_HEADER, login)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            text = r.read().decode()
            return r.status, (text if raw else json.loads(text))
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def test_reminders_are_owner_only_end_to_end(hub):
    import time as _t

    addr, svc = hub
    body = {"text": "call mom", "due": _t.time() + 600}
    assert _call(addr, "POST", "/v1/reminders", body)[0] == 403
    assert _call(addr, "POST", "/v1/reminders", body, login="evil@x.com")[0] == 403
    status, out = _call(addr, "POST", "/v1/reminders", body, login="manav@example.com")
    assert status == 200 and out["ok"]
    status, listed = _call(addr, "POST", "/v1/reminders/list", {}, login="manav@example.com")
    assert [r["text"] for r in listed["reminders"]] == ["call mom"]
    r = svc.reminders.get(out["id"])
    assert _call(addr, "GET", f"/v1/ack/{r['id']}?t={r['token']}", raw=True)[0] == 403
    status, page = _call(addr, "GET", f"/v1/ack/{r['id']}?t={r['token']}", login="manav@example.com", raw=True)
    assert status == 200 and "call mom" in page
    status, out = _call(addr, "POST", "/v1/reminders/cancel", {"id": "all"}, login="manav@example.com")
    assert out["cancelled"] == 0  # the ack already finished it


# --- laptop skills -----------------------------------------------------------------------

class _HubEgress:
    def __init__(self, reply=None, exc=None):
        self.reply, self.exc, self.calls = reply or {"ok": True, "alerts_ready": True}, exc, []

    def post_json(self, url, body, *, headers, timeout=20.0, method="POST"):
        self.calls.append((url, body))
        if self.exc:
            raise self.exc
        return self.reply


def _ctx(egress, host="nova-hub.tail1.ts.net"):
    cfg = dataclasses.replace(Config(), hub=dataclasses.replace(HubConfig(), host=host))
    return SkillCtx(config=cfg, ladder=None, egress=egress, armed_by=ArmedBy.VOICE)


async def test_remind_me_sends_the_time_to_the_hub():
    eg = _HubEgress()
    reply = await rem_skills.remind_me(_ctx(eg), text="call mom", when="in 10 minutes")
    url, body = eg.calls[0]
    assert url == "https://nova-hub.tail1.ts.net/v1/reminders"
    assert body["text"] == "call mom" and body["urgent"] is False
    assert abs(body["due"] - (datetime.now() + timedelta(minutes=10)).timestamp()) < 5
    assert reply.success and "in 10 minutes" in reply.text


async def test_remind_me_urgent_and_alerts_not_set_up():
    eg = _HubEgress({"ok": True, "alerts_ready": False})
    reply = await rem_skills.remind_me(_ctx(eg), text="leave", when="in 5 minutes", urgent="yes")
    assert eg.calls[0][1]["urgent"] is True and "alerts aren't set up" in reply.text


async def test_unclear_time_asks_instead_of_guessing():
    eg = _HubEgress()
    reply = await rem_skills.remind_me(_ctx(eg), text="call mom", when="sometime")
    assert not reply.success and "When" in reply.text and eg.calls == []


async def test_no_hub_says_so():
    reply = await rem_skills.remind_me(_ctx(_HubEgress(), host=""), text="x", when="in 5 minutes")
    assert reply.text == rem_skills.NO_HUB
    reply = await rem_skills.set_timer(_ctx(_HubEgress(exc=EgressError("down"))), duration="5 minutes")
    assert reply.text == rem_skills.NO_HUB


async def test_timer_list_and_cancel():
    eg = _HubEgress()
    assert (await rem_skills.set_timer(_ctx(eg), duration="10 minutes")).text.startswith("Timer set for 10 minutes")
    assert eg.calls[0][1]["text"] == "Your 10 minutes timer is done."
    due = (datetime.now() + timedelta(minutes=10)).timestamp()
    eg2 = _HubEgress({"reminders": [{"id": "a", "text": "call mom", "due": due, "urgent": False}]})
    assert "call mom in 10 minutes" in (await rem_skills.list_reminders(_ctx(eg2))).text
    eg3 = _HubEgress({"ok": True, "cancelled": 2})
    assert (await rem_skills.cancel_reminders(_ctx(eg3))).text == "Done, nothing's left."
    assert eg3.calls[0][1] == {"id": "all"}


@pytest.mark.parametrize("text,skill,params", [
    ("remind me to call mom at 7", "remind_me", {"text": "call mom", "when": "at 7"}),
    ("remind me in 20 minutes to take the laundry out", "remind_me",
     {"when": "in 20 minutes", "text": "take the laundry out"}),
    ("remind me tomorrow at 9 to submit the assignment", "remind_me",
     {"when": "tomorrow at 9", "text": "submit the assignment"}),
    ("set a timer for 10 minutes", "set_timer", {"duration": "10 minutes"}),
    ("what are my reminders", "list_reminders", {}),
    ("cancel all my reminders", "cancel_reminders", {}),
])
def test_routing(text, skill, params):
    m = Router(confidence_floor=0.42, ambiguity_margin=0.08).route(text)
    assert m.spec is not None and m.spec.name == skill and m.params == params


def test_the_hub_lane_may_take_a_simple_reminder():
    from nova.brain.hub_tier import check
    from nova.skills.registry import all_skills

    d = {"route": "tool", "tool": "remind_me", "params": {"text": "call mom", "when": "at 7"}, "say": ""}
    assert check(d, "remind me to call mom at 7", all_skills()) is not None
    d2 = dict(d, params={"text": "call dad", "when": "at 7"})
    assert check(d2, "remind me to call mom at 7", all_skills()) is None
    assert get_skill("remind_me") is not None
