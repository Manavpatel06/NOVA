"""NOVA on a real phone number (ADR 0025): call NOVA, or NOVA calls you.

Twilio carries the call. Its own speech recognition turns what Manav says into
text (so call audio passes through Twilio; Manav accepted that for calls). The
hub's NOVA answers, and Twilio reads the answer out.

Twilio reaches the hub through Tailscale Funnel on port 8443, mounted on
/phone only. Funnel is the one thing on the hub reachable from the internet,
so every webhook must pass, in order:
  1. Twilio's signature (HMAC-SHA1 of the exact public URL and the form, with
     Manav's auth token; constant-time compare). Anything else gets 403.
  2. The call is to or from NOVA's own number and Manav's own number.
  3. A 6-digit PIN typed on the keypad before NOVA will do anything (caller ID
     can be spoofed; the PIN can't). Five wrong PINs lock calls for an hour.
Actions asked for on a call are prefix voice (ArmedBy.VOICE): anything risky
still needs "Nova, confirm".

Twilio expects an answer within 15 seconds, so a sentence that takes NOVA
longer gets "one moment" and a redirect to /phone/wait until it's ready.

Credentials live in data/phone.json (0600), written by `nova-hub phone`,
which Manav runs and types into himself; nothing here prints or logs them.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
from pathlib import Path
from typing import Any, Callable
from xml.sax.saxutils import escape, quoteattr

from nova.log import get_logger

_log = get_logger("hub.phone")

API_HOST = "api.twilio.com"
FUNNEL_PORT = 8443
CALL_SID_RE = re.compile(r"CA[0-9a-f]{32}")
E164_RE = re.compile(r"\+[1-9][0-9]{6,14}")
PIN_TRIES = 5
LOCK_S = 3600.0
REPLY_WAIT_S = 9.0       # leave Twilio's 15 s budget some room
CALL_TTL_S = 2 * 3600.0
VOICE = "Polly.Joanna"


class PhoneError(ValueError):
    pass


def hash_pin(pin: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", pin.encode(), bytes.fromhex(salt), 200_000).hex()


def twilio_signature(token: str, url: str, form: dict[str, str]) -> str:
    """Twilio's request signature: HMAC-SHA1 over the URL then each POST
    field (sorted by name) as name+value, base64."""
    payload = url + "".join(k + form[k] for k in sorted(form))
    return base64.b64encode(hmac.new(token.encode(), payload.encode(), hashlib.sha1).digest()).decode()


def _twiml(*parts: str) -> str:
    return '<?xml version="1.0" encoding="UTF-8"?><Response>' + "".join(parts) + "</Response>"


def _say(text: str) -> str:
    text = " ".join((text or "").split())[:1500]
    return f"<Say voice={quoteattr(VOICE)}>{escape(text)}</Say>" if text else ""


class Phone:
    def __init__(self, talk: Any, *, data_dir: Path, hub_name: Callable[[], str], egress: Any = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.talk = talk
        self._path = Path(data_dir) / "phone.json"
        self._hub_name = hub_name
        self._egress = egress
        self._clock = clock
        self._authed: dict[str, float] = {}      # CallSid -> since
        self._failures: list[float] = []
        self._locked_until = 0.0
        self._outbound: dict[str, str] = {}      # one-time token -> what NOVA says first
        self._lock = threading.Lock()

    # --- settings -----------------------------------------------------------

    def settings(self) -> dict[str, str] | None:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        need = ("account_sid", "auth_token", "nova_number", "owner_number", "pin_hash", "pin_salt")
        if not isinstance(data, dict) or not all(isinstance(data.get(k), str) and data[k] for k in need):
            return None
        return data

    def configured(self) -> bool:
        return self.settings() is not None

    def base_url(self) -> str:
        return f"https://{self._hub_name()}:{FUNNEL_PORT}/phone"

    # --- webhooks (public, via Funnel) ------------------------------------------

    def webhook(self, route: str, query: str, form: dict[str, str], signature: str | None) -> tuple[int, str]:
        """Returns (HTTP status, TwiML)."""
        cfg = self.settings()
        if cfg is None:
            return 404, _twiml("<Reject/>")
        url = f"{self.base_url()}/{route}" + (f"?{query}" if query else "")
        expected = twilio_signature(cfg["auth_token"], url, form)
        if not signature or not hmac.compare_digest(expected, signature):
            _log.warning("phone: refused a request without Twilio's signature")
            return 403, _twiml("<Reject/>")
        sid = form.get("CallSid", "")
        if not CALL_SID_RE.fullmatch(sid):
            return 400, _twiml("<Hangup/>")
        parties = {form.get("From", ""), form.get("To", "")}
        if parties != {cfg["nova_number"], cfg["owner_number"]}:
            _log.warning("phone: refused a call that isn't between NOVA's number and Manav's")
            return 200, _twiml("<Reject/>")
        if route == "voice":
            return 200, self._answer(sid, query)
        if route == "pin":
            return 200, self._pin(sid, form.get("Digits", ""), cfg)
        if route == "status":
            if form.get("CallStatus") in ("completed", "busy", "failed", "no-answer", "canceled"):
                with self._lock:
                    self._authed.pop(sid, None)
            return 200, _twiml()
        if not self._is_authed(sid):
            return 200, _twiml(_say("Enter your PIN first."), self._gather_pin())
        if route == "heard":
            heard = " ".join(form.get("SpeechResult", "").split())[:500]
            if not heard:
                return 200, _twiml(self._gather_speech(_say("I didn't catch that.")))
            words, awaiting = self.talk.phone_say(sid, heard, REPLY_WAIT_S)
            return 200, self._reply(words, awaiting)
        if route == "wait":
            words, awaiting = self.talk.phone_wait(sid, REPLY_WAIT_S)
            return 200, self._reply(words, awaiting)
        return 404, _twiml("<Hangup/>")

    def _answer(self, sid: str, query: str) -> str:
        if self._clock() < self._locked_until:
            return _twiml(_say("NOVA isn't taking calls right now."), "<Hangup/>")
        first = ""
        m = re.fullmatch(r"msg=([A-Za-z0-9_-]{16,40})", query or "")
        if m:
            with self._lock:
                first = self._outbound.pop(m.group(1), "")
        intro = _say(first) if first else _say("NOVA here.")
        return _twiml(intro, self._gather_pin(_say("Enter your PIN to talk.")))

    def _pin(self, sid: str, digits: str, cfg: dict[str, str]) -> str:
        now = self._clock()
        if now < self._locked_until:
            return _twiml("<Hangup/>")
        ok = bool(re.fullmatch(r"[0-9]{6}", digits or "")) and hmac.compare_digest(
            hash_pin(digits, cfg["pin_salt"]), cfg["pin_hash"])
        if not ok:
            with self._lock:
                self._failures = [t for t in self._failures if now - t < LOCK_S] + [now]
                if len(self._failures) >= PIN_TRIES:
                    self._locked_until = now + LOCK_S
                    _log.warning("phone: %d wrong PINs; calls locked for an hour", PIN_TRIES)
                    return _twiml(_say("Too many wrong PINs."), "<Hangup/>")
            return _twiml(self._gather_pin(_say("That's not it. Try again.")))
        with self._lock:
            self._failures.clear()
            self._authed = {k: v for k, v in self._authed.items() if now - v < CALL_TTL_S}
            self._authed[sid] = now
        return _twiml(self._gather_speech(_say("Hi Manav. What can I do?")))

    def _is_authed(self, sid: str) -> bool:
        with self._lock:
            since = self._authed.get(sid)
        return since is not None and self._clock() - since < CALL_TTL_S

    def _gather_pin(self, prompt: str = "") -> str:
        return (f'<Gather input="dtmf" numDigits="6" timeout="10" action={quoteattr(self.base_url() + "/pin")}>'
                f"{prompt}</Gather>" + _say("Goodbye.") + "<Hangup/>")

    def _gather_speech(self, prompt: str = "") -> str:
        return (f'<Gather input="speech" speechTimeout="auto" language="en-US" '
                f'action={quoteattr(self.base_url() + "/heard")}>{prompt}</Gather>'
                + self._gather_again())

    def _gather_again(self) -> str:
        # silence after a reply: listen once more, then hang up
        return (f'<Gather input="speech" speechTimeout="auto" language="en-US" '
                f'action={quoteattr(self.base_url() + "/heard")}></Gather>' + _say("Talk soon.") + "<Hangup/>")

    def _reply(self, words: str | None, awaiting: bool) -> str:
        if words is None:  # still thinking
            return _twiml(_say("One moment."), '<Pause length="1"/>',
                          f"<Redirect method=\"POST\">{escape(self.base_url() + '/wait')}</Redirect>")
        return _twiml(self._gather_speech(_say(words)))

    # --- NOVA calls Manav ----------------------------------------------------------

    def call_me(self, message: str) -> dict[str, Any]:
        cfg = self.settings()
        if cfg is None:
            raise PhoneError("phone calls aren't set up (run: nova-hub phone)")
        if self._egress is None:
            raise PhoneError("no network access configured for calls")
        token = secrets.token_urlsafe(18)
        with self._lock:
            self._outbound[token] = " ".join((message or "").split())[:500]
        url = f"https://{API_HOST}/2010-04-01/Accounts/{cfg['account_sid']}/Calls.json"
        auth = base64.b64encode(f"{cfg['account_sid']}:{cfg['auth_token']}".encode()).decode()
        out = self._egress.post_form(url, {
            "To": cfg["owner_number"], "From": cfg["nova_number"],
            "Url": f"{self.base_url()}/voice?msg={token}",
            "StatusCallback": f"{self.base_url()}/status",
        }, headers={"authorization": f"Basic {auth}"}, timeout=20.0)
        return {"ok": True, "status": str(out.get("status", "queued"))[:20]}


def write_settings(path: Path, *, account_sid: str, auth_token: str, nova_number: str, owner_number: str,
                   pin: str) -> None:
    """Used by `nova-hub phone` (values typed by Manav on the hub)."""
    if not re.fullmatch(r"AC[0-9a-f]{32}", account_sid):
        raise PhoneError("the Account SID starts with AC and has 32 more letters/digits")
    if not re.fullmatch(r"[0-9a-f]{32}", auth_token):
        raise PhoneError("the Auth Token is 32 letters/digits")
    for n in (nova_number, owner_number):
        if not E164_RE.fullmatch(n):
            raise PhoneError(f"{n!r} isn't a phone number like +14805551234")
    if not re.fullmatch(r"[0-9]{6}", pin):
        raise PhoneError("the PIN is 6 digits")
    salt = secrets.token_hex(16)
    data = {"account_sid": account_sid, "auth_token": auth_token, "nova_number": nova_number,
            "owner_number": owner_number, "pin_salt": salt, "pin_hash": hash_pin(pin, salt)}
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f)
