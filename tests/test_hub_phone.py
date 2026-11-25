"""NOVA on a real phone number (ADR 0025): Twilio's signature, caller check,
PIN, the call conversation, NOVA calling out, and what Funnel can reach."""
from __future__ import annotations

import json
import os
import re
import stat
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import pytest

from nova.hub.phone import (PIN_TRIES, Phone, PhoneError, hash_pin, twilio_signature, write_settings)
from nova.hub.talk import Talk

TOKEN = "0123456789abcdef0123456789abcdef"
SID = "AC" + "1" * 32
NOVA = "+14805550100"
ME = "+14805550199"
NAME = "nova-hub.tail1.ts.net"
CALL = "CA" + "a" * 32
BASE = f"https://{NAME}:8443/phone"


def _settings(tmp_path, pin="123456"):
    write_settings(tmp_path / "phone.json", account_sid=SID, auth_token=TOKEN, nova_number=NOVA,
                   owner_number=ME, pin=pin)


class _Talk:
    def __init__(self, reply="It's 7 PM.", slow=False):
        self.said = []
        self.reply = reply
        self.slow = slow

    def phone_say(self, sid, heard, wait_s):
        self.said.append(heard)
        return (None, False) if self.slow else (self.reply, False)

    def phone_wait(self, sid, wait_s):
        return self.reply, False


class _Egress:
    def __init__(self):
        self.posts = []

    def post_form(self, url, fields, *, headers, timeout=20.0):
        self.posts.append((url, fields, headers))
        return {"status": "queued"}


def _phone(tmp_path, talk=None, egress=None, clock=time.monotonic):
    return Phone(talk or _Talk(), data_dir=tmp_path, hub_name=lambda: NAME, egress=egress, clock=clock)


def _hook(phone, route, form, query="", sig=None):
    form = {"CallSid": CALL, "From": ME, "To": NOVA, **form}
    url = f"{BASE}/{route}" + (f"?{query}" if query else "")
    return phone.webhook(route, query, form, sig if sig is not None else twilio_signature(TOKEN, url, form))


# --- the gate ------------------------------------------------------------------------

def test_twilio_signature_matches_the_documented_algorithm():
    # the test vector Twilio's own libraries use (twilio-python test_validation)
    url = "https://mycompany.com/myapp.php?foo=1&bar=2"
    form = {"CallSid": "CA1234567890ABCDE", "Caller": "+12349013030", "Digits": "1234", "From": "+12349013030",
            "To": "+18005551212"}
    assert twilio_signature("12345", url, form) == "0/KCTR6DLpKmkAf8muzZqo1nDgQ="


def test_nothing_happens_until_set_up(tmp_path):
    assert _hook(_phone(tmp_path), "voice", {})[0] == 404


@pytest.mark.parametrize("sig", ["", "AAAA", twilio_signature("wrong-token", f"{BASE}/voice", {})])
def test_requests_without_twilios_signature_are_refused(tmp_path, sig):
    _settings(tmp_path)
    status, twiml = _hook(_phone(tmp_path), "voice", {}, sig=sig)
    assert status == 403 and "<Reject/>" in twiml


def test_a_signature_for_another_url_or_changed_form_fails(tmp_path):
    _settings(tmp_path)
    form = {"CallSid": CALL, "From": ME, "To": NOVA}
    sig = twilio_signature(TOKEN, f"{BASE}/voice", form)
    assert _phone(tmp_path).webhook("heard", "", form, sig)[0] == 403
    assert _phone(tmp_path).webhook("voice", "", {**form, "From": "+19998887777"}, sig)[0] == 403


def test_calls_between_other_numbers_are_rejected(tmp_path):
    _settings(tmp_path)
    status, twiml = _hook(_phone(tmp_path), "voice", {"From": "+19998887777"})
    assert status == 200 and "<Reject/>" in twiml


def test_the_pin_comes_before_anything(tmp_path):
    _settings(tmp_path)
    talk = _Talk()
    phone = _phone(tmp_path, talk)
    _, twiml = _hook(phone, "voice", {})
    assert 'input="dtmf"' in twiml and "/phone/pin" in twiml
    _, twiml = _hook(phone, "heard", {"SpeechResult": "shut down my laptop"})
    assert "PIN" in twiml and talk.said == []          # not acted on
    _, twiml = _hook(phone, "pin", {"Digits": "000000"})
    assert "not it" in twiml
    _, twiml = _hook(phone, "pin", {"Digits": "123456"})
    assert 'input="speech"' in twiml
    _, twiml = _hook(phone, "heard", {"SpeechResult": "what time is it"})
    assert talk.said == ["what time is it"] and "It's 7 PM." in twiml


def test_too_many_wrong_pins_lock_calls_for_an_hour(tmp_path):
    _settings(tmp_path)
    now = [1000.0]
    phone = _phone(tmp_path, clock=lambda: now[0])
    for _ in range(PIN_TRIES - 1):
        assert "not it" in _hook(phone, "pin", {"Digits": "999999"})[1]
    assert "Too many" in _hook(phone, "pin", {"Digits": "999999"})[1]
    assert "<Hangup/>" in _hook(phone, "pin", {"Digits": "123456"})[1]   # even the right PIN
    assert "isn't taking calls" in _hook(phone, "voice", {})[1]
    now[0] += 3601
    assert 'input="speech"' in _hook(phone, "pin", {"Digits": "123456"})[1]


def test_a_finished_call_forgets_its_pin(tmp_path):
    _settings(tmp_path)
    phone = _phone(tmp_path)
    _hook(phone, "pin", {"Digits": "123456"})
    _hook(phone, "status", {"CallStatus": "completed"})
    assert "PIN" in _hook(phone, "heard", {"SpeechResult": "hello"})[1]


def test_replies_are_escaped(tmp_path):
    _settings(tmp_path)
    phone = _phone(tmp_path, _Talk(reply='</Say><Dial>+19990001111</Dial> & "x"'))
    _hook(phone, "pin", {"Digits": "123456"})
    twiml = _hook(phone, "heard", {"SpeechResult": "hi"})[1]
    assert "<Dial>" not in twiml and "&lt;/Say&gt;&lt;Dial&gt;" in twiml


def test_a_slow_answer_waits_politely(tmp_path):
    _settings(tmp_path)
    phone = _phone(tmp_path, _Talk(slow=True))
    _hook(phone, "pin", {"Digits": "123456"})
    twiml = _hook(phone, "heard", {"SpeechResult": "write me a poem"})[1]
    assert "One moment" in twiml and f"{BASE}/wait" in twiml
    assert "It's 7 PM." in _hook(phone, "wait", {})[1]


# --- NOVA calls Manav -------------------------------------------------------------------

def test_nova_calls_manavs_number_from_its_own_and_says_the_message_once(tmp_path):
    _settings(tmp_path)
    eg = _Egress()
    phone = _phone(tmp_path, egress=eg)
    phone.call_me("Time to leave for class.")
    url, fields, headers = eg.posts[0]
    assert url == f"https://api.twilio.com/2010-04-01/Accounts/{SID}/Calls.json"
    assert fields["To"] == ME and fields["From"] == NOVA and headers["authorization"].startswith("Basic ")
    query = fields["Url"].split("?", 1)[1]
    assert "Time to leave" not in fields["Url"]                    # the words never travel in the URL
    twiml = _hook(phone, "voice", {"From": NOVA, "To": ME}, query=query)[1]
    assert "Time to leave for class." in twiml and "PIN" in twiml
    assert "Time to leave" not in _hook(phone, "voice", {"From": NOVA, "To": ME}, query=query)[1]  # once


def test_calling_needs_setup(tmp_path):
    with pytest.raises(PhoneError):
        _phone(tmp_path, egress=_Egress()).call_me("hi")


# --- settings -----------------------------------------------------------------------------

def test_settings_are_private_and_the_pin_is_never_stored(tmp_path):
    _settings(tmp_path, pin="482913")
    raw = (tmp_path / "phone.json").read_text()
    assert "482913" not in raw
    data = json.loads(raw)
    assert data["pin_hash"] == hash_pin("482913", data["pin_salt"])
    if os.name == "posix":
        assert stat.S_IMODE((tmp_path / "phone.json").stat().st_mode) == 0o600


@pytest.mark.parametrize("field,value", [
    ("account_sid", "SK123"), ("auth_token", "short"), ("nova_number", "4805551234"),
    ("owner_number", "+1 480 555"), ("pin", "12345"), ("pin", "abcdef"),
])
def test_bad_settings_are_refused(tmp_path, field, value):
    values = dict(account_sid=SID, auth_token=TOKEN, nova_number=NOVA, owner_number=ME, pin="123456")
    values[field] = value
    with pytest.raises(PhoneError):
        write_settings(tmp_path / "phone.json", **values)


# --- the talk loop for calls ------------------------------------------------------------------

def test_phone_turns_return_none_while_thinking_then_the_answer():
    gate = threading.Event()

    class Slow:
        async def turn(self, text, channel):
            import asyncio

            await asyncio.to_thread(gate.wait, 5)
            return f"answered {text} ({channel.armed_by.name})"

        async def warm_up(self):
            pass

    talk = Talk(Slow())
    assert talk.phone_say(CALL, "hello", 0.2) == (None, False)
    gate.set()
    words, awaiting = talk.phone_wait(CALL, 3)
    assert words == "answered hello (VOICE)" and awaiting is False


# --- what the internet can reach through Funnel ---------------------------------------------------

@pytest.fixture
def hub(tmp_path):
    from nova.hub.listener import make_server
    from nova.hub.service import HubService

    (tmp_path / "hub_owner").write_text("manav@example.com\n")
    (tmp_path / "hub_name").write_text(NAME + "\n")
    _settings(tmp_path)

    class _M:
        model = "m"

    svc = HubService(model=_M(), data_dir=tmp_path, phone=_phone(tmp_path), talk=Talk(_Echo()))
    server = make_server(svc, 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server.server_address
    server.shutdown()
    server.server_close()


class _Echo:
    async def turn(self, text, channel):
        return text

    async def warm_up(self):
        pass


def _send(addr, method, path, data=None, ctype=None, **headers):
    req = urllib.request.Request(f"http://{addr[0]}:{addr[1]}{path}", data=data, method=method)
    if ctype:
        req.add_header("Content-Type", ctype)
    for k, v in headers.items():
        req.add_header(k.replace("_", "-"), v)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def test_twilio_reaches_the_phone_routes_through_funnel(hub):
    form = {"CallSid": CALL, "From": ME, "To": NOVA}
    body = urllib.parse.urlencode(form).encode()
    status, twiml = _send(hub, "POST", "/v1/phone/voice", body, "application/x-www-form-urlencoded",
                          Host=f"{NAME}:8443", Tailscale_Funnel_Request="?1",
                          X_Twilio_Signature=twilio_signature(TOKEN, f"{BASE}/voice", form))
    assert status == 200 and twiml.startswith("<?xml") and "PIN" in twiml


def test_funnel_traffic_never_reaches_anything_else(hub):
    funnel = {"Host": f"{NAME}:8443", "Tailscale_Funnel_Request": "?1", "Tailscale_User_Login": "manav@example.com"}
    body = json.dumps({"session": "abcdefgh", "text": "hi"}).encode()
    assert _send(hub, "POST", "/v1/talk", body, "application/json", **funnel)[0] == 403
    assert _send(hub, "POST", "/v1/talk", body, "application/json",
                 **{**funnel, "Host": NAME})[0] == 403                 # even claiming the owner's name
    assert _send(hub, "GET", "/talk", **funnel)[0] == 404
    assert _send(hub, "GET", "/v1/health", **funnel)[0] == 404
    assert _send(hub, "PUT", "/v1/memory", b"{}", "application/json", **funnel)[0] == 404
    assert _send(hub, "POST", "/v1/phone/call", json.dumps({"message": "x"}).encode(), "application/json",
                 **funnel)[0] == 403                                   # placing calls is owner-only


@pytest.mark.parametrize("path", ["/v1/phone/voice/../talk", "/v1/phone/admin", "/v1/phone/voice?x=<script>"])
def test_odd_phone_paths_go_nowhere(hub, path):
    status, _ = _send(hub, "POST", path, b"a=1", "application/x-www-form-urlencoded",
                      Host=f"{NAME}:8443", Tailscale_Funnel_Request="?1")
    assert status in (403, 404)


def test_phone_routes_check_the_host_and_body(hub):
    form = urllib.parse.urlencode({"CallSid": CALL}).encode()
    assert _send(hub, "POST", "/v1/phone/voice", form, "application/x-www-form-urlencoded", Host="evil.com")[0] == 403
    assert _send(hub, "POST", "/v1/phone/voice", form, "application/json", Host=f"{NAME}:8443")[0] == 400
    big = b"a=" + b"x" * 20000
    assert _send(hub, "POST", "/v1/phone/voice", big, "application/x-www-form-urlencoded",
                 Host=f"{NAME}:8443")[0] == 400


# --- reminders that call ------------------------------------------------------------------------

def test_a_call_reminder_phones_once(tmp_path):
    from nova.hub.service import HubService

    calls = []

    class _P:
        def configured(self):
            return True

        def call_me(self, message):
            calls.append(message)

    class _M:
        model = "m"

    now = [1_700_000_000.0]
    svc = HubService(model=_M(), data_dir=tmp_path, phone=_P(), clock=lambda: now[0])
    svc.add_reminder({"text": "wake up", "due": now[0] + 60, "call": True})
    svc.add_reminder({"text": "quiet one", "due": now[0] + 60})
    now[0] += 61
    svc.tick()
    svc.tick()
    assert calls == ["This is your reminder: wake up"]


# --- egress: form posts follow the same allowlist --------------------------------------------------

def test_form_posts_only_to_allowlisted_https_hosts(tmp_path):
    from nova.security.audit import AuditLog
    from nova.security.egress import Egress, EgressError

    eg = Egress(["api.twilio.com"], audit=AuditLog(tmp_path / "a.log"))
    for url in ("https://evil.example/x", "http://api.twilio.com/x"):
        with pytest.raises(EgressError):
            eg.post_form(url, {"a": "b"}, headers={})
    log = (tmp_path / "a.log").read_text()
    assert '"allowed":false' in log and "authorization" not in log.lower()
