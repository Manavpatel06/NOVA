"""Talking to the hub's NOVA from the phone's talk page (ADR 0023).

One request = one thing Manav said (recorded audio or typed text). The reply
is what NOVA says next: the answer, or an approval question ("Should I ...?")
-- then the NEXT request is the answer to it, because the turn is still
running and waiting.

Turns run on this module's own asyncio loop (a thread), like BrainHost; the
HTTP threads hand work over and wait for the next thing NOVA says. Audio is
transcribed and synthesized in the HTTP thread (HubVoice serializes itself)
and never stored.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import re
import threading
import time
from typing import Any

from nova.hub.service import HubError
from nova.log import get_logger

_log = get_logger("hub.talk")

SESSION_RE = re.compile(r"[A-Za-z0-9_-]{8,40}")
MAX_TEXT = 1000
ANSWER_WAIT_S = 90.0     # how long an approval question waits for an answer
REPLY_WAIT_S = 170.0     # how long one request waits for NOVA's next words
MAX_SESSIONS = 8


FOLLOW_UP_S = 8.0        # after a bare "Nova": the next sentence needs no name


class _Session:
    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self.events: asyncio.Queue = asyncio.Queue()
        self.answer: asyncio.Future | None = None
        self.task: asyncio.Task | None = None
        self.used = time.monotonic()
        self.follow_until = 0.0


class _PageChannel:
    def __init__(self, session: _Session, *, name: str = "the phone", armed_by: Any = None) -> None:
        from nova.security.tiers import ArmedBy

        self._s = session
        self.name = name
        # a tap on the page is a deliberate press; a mic that's always open is
        # prefix voice (ArmedBy.VOICE: CONFIRM still needs "Nova, confirm")
        self.armed_by = armed_by or ArmedBy.PTT

    async def ask(self, prompt: str) -> "str | None":
        loop = asyncio.get_running_loop()
        self._s.answer = loop.create_future()
        await self._s.events.put(("ask", prompt))
        try:
            return await asyncio.wait_for(asyncio.shield(self._s.answer), ANSWER_WAIT_S)
        except asyncio.TimeoutError:
            return None
        finally:
            self._s.answer = None


class Talk:
    def __init__(self, assistant: Any, voice: Any | None = None) -> None:
        self.assistant = assistant
        self.voice = voice
        self._sessions: dict[str, _Session] = {}
        self._guard = threading.Lock()
        self._loop = asyncio.new_event_loop()
        threading.Thread(target=self._loop.run_forever, name="talk", daemon=True).start()

    def voice_ready(self) -> bool:
        return self.voice is not None and self.voice.ready()

    def warm_up(self) -> None:
        """Load the voice and start the Claude session in the background."""
        def run() -> None:
            if self.voice_ready():
                try:
                    self.voice.warm_up()
                except Exception as exc:  # noqa: BLE001
                    _log.warning("voice warm-up failed (%s)", type(exc).__name__)
            try:
                asyncio.run_coroutine_threadsafe(self.assistant.warm_up(), self._loop).result(120)
            except Exception as exc:  # noqa: BLE001
                _log.warning("brain warm-up failed (%s)", type(exc).__name__)

        threading.Thread(target=run, name="talk-warmup", daemon=True).start()

    # --- one request ------------------------------------------------------

    def handle(self, body: Any) -> dict[str, Any]:
        if not isinstance(body, dict):
            raise HubError("body must be a JSON object")
        sid = body.get("session")
        if not isinstance(sid, str) or not SESSION_RE.fullmatch(sid):
            raise HubError("session must be 8-40 letters, digits, - or _")
        heard = self._heard(body)
        session = self._session(sid)
        if session.answer is not None and not session.answer.done():
            # this is the answer to the question NOVA just asked
            self._loop.call_soon_threadsafe(_set, session.answer, heard)
        elif session.task is not None and not session.task.done():
            return self._out(heard, "Still working on the last one.", awaiting=False, want_audio=body.get("speak"))
        elif not heard:
            return self._out("", "I didn't catch that.", awaiting=False, want_audio=body.get("speak"))
        else:
            fut = asyncio.run_coroutine_threadsafe(self._start(session, heard), self._loop)
            fut.result(5)
        kind, words = asyncio.run_coroutine_threadsafe(self._next(session), self._loop).result(REPLY_WAIT_S + 5)
        return self._out(heard, words, awaiting=kind == "ask", want_audio=body.get("speak"))

    def hear(self, body: Any) -> dict[str, Any]:
        """One sentence from a device's always-open mic (the laptop through
        NOVA Link, or the hub's own). Acted on only when it starts with
        NOVA's name, answers NOVA's question, or follows a bare "Nova".
        Anything else is dropped: {"ignored": true}, its words never logged
        or returned."""
        from nova.hub.devices import DEVICE_RE

        if not isinstance(body, dict):
            raise HubError("body must be a JSON object")
        device = body.get("device")
        if not isinstance(device, str) or not DEVICE_RE.fullmatch(device):
            raise HubError("device must be a short lowercase name like 'laptop'")
        if not isinstance(body.get("audio"), str):
            raise HubError("send audio")
        return self.hear_text(device, self._heard({"audio": body["audio"]}), want_audio=body.get("speak"))

    def hear_text(self, device: str, heard: str, *, want_audio: Any = True) -> dict[str, Any]:
        from nova.brain.address import strip_address
        from nova.security.tiers import ArmedBy

        session = self._session(f"device-{device}")
        heard = (heard or "").strip()
        if session.answer is not None and not session.answer.done():
            if not heard:
                return {"ignored": True}
            self._loop.call_soon_threadsafe(_set, session.answer, heard)
        else:
            if session.task is not None and not session.task.done():
                return {"ignored": True}  # mid-turn chatter is not a new command
            cfg = getattr(self.assistant, "config", None)
            names = list(cfg.wake.names) if cfg is not None else ["nova"]
            command = strip_address(heard, names)
            if time.monotonic() < session.follow_until:
                session.follow_until = 0.0
                command = heard if command is None else command
            if command is None or not heard:
                return {"ignored": True}
            if command == "":
                session.follow_until = time.monotonic() + FOLLOW_UP_S
                return self._out("", "Yes?", awaiting=True, want_audio=want_audio)
            channel = _PageChannel(session, name=f"your {device}", armed_by=ArmedBy.VOICE)
            asyncio.run_coroutine_threadsafe(self._start(session, command, channel), self._loop).result(5)
            heard = command
        kind, words = asyncio.run_coroutine_threadsafe(self._next(session), self._loop).result(REPLY_WAIT_S + 5)
        return self._out(heard, words, awaiting=kind == "ask", want_audio=want_audio)

    def _heard(self, body: dict) -> str:
        if isinstance(body.get("text"), str):
            return " ".join(body["text"].split())[:MAX_TEXT]
        audio = body.get("audio")
        if not isinstance(audio, str):
            raise HubError("send text or audio")
        if not self.voice_ready():
            raise HubError("voice isn't set up on the hub yet (run: nova-hub voice); type instead")
        try:
            pcm = base64.b64decode(audio, validate=True)
        except (binascii.Error, ValueError):
            raise HubError("audio must be base64") from None
        try:
            return self.voice.transcribe(pcm)
        except ValueError as exc:
            raise HubError(str(exc)) from None

    def _session(self, sid: str) -> _Session:
        with self._guard:
            s = self._sessions.get(sid)
            if s is None:
                if len(self._sessions) >= MAX_SESSIONS:
                    idle = [k for k, v in self._sessions.items() if v.task is None or v.task.done()]
                    if not idle:
                        raise HubError("too many conversations at once")
                    del self._sessions[min(idle, key=lambda k: self._sessions[k].used)]
                s = self._sessions[sid] = _Session(self._loop)
            s.used = time.monotonic()
            return s

    # --- a phone call (ADR 0025): Twilio's webhooks can't wait long ----------

    def phone_say(self, call_sid: str, heard: str, wait_s: float) -> "tuple[str | None, bool]":
        """Something said on the call. Returns (words, awaiting); words None
        means NOVA is still thinking -- ask again with phone_wait()."""
        from nova.security.tiers import ArmedBy

        session = self._session(f"phone-{call_sid}")
        if session.answer is not None and not session.answer.done():
            self._loop.call_soon_threadsafe(_set, session.answer, heard)
        elif session.task is None or session.task.done():
            channel = _PageChannel(session, name="a phone call", armed_by=ArmedBy.VOICE)
            asyncio.run_coroutine_threadsafe(self._start(session, heard, channel), self._loop).result(5)
        return self._within(session, wait_s)

    def phone_wait(self, call_sid: str, wait_s: float) -> "tuple[str | None, bool]":
        return self._within(self._session(f"phone-{call_sid}"), wait_s)

    def _within(self, session: _Session, wait_s: float) -> "tuple[str | None, bool]":
        async def get():
            return await asyncio.wait_for(session.events.get(), wait_s)

        try:
            kind, words = asyncio.run_coroutine_threadsafe(get(), self._loop).result(wait_s + 2)
        except (asyncio.TimeoutError, TimeoutError):
            if session.task is None or session.task.done():
                return "", False
            return None, False
        return words, kind == "ask"

    async def _start(self, session: _Session, text: str, channel: Any = None) -> None:
        while not session.events.empty():  # a reply nobody waited for (an earlier timeout)
            session.events.get_nowait()

        async def run() -> None:
            try:
                reply = await self.assistant.turn(text, channel or _PageChannel(session))
            except Exception as exc:  # noqa: BLE001 -- one bad turn never takes the hub down
                _log.error("turn failed (%s)", type(exc).__name__)
                reply = "Sorry, something went wrong."
            await session.events.put(("done", reply))

        session.task = asyncio.get_running_loop().create_task(run())

    async def _next(self, session: _Session) -> tuple[str, str]:
        try:
            return await asyncio.wait_for(session.events.get(), REPLY_WAIT_S)
        except asyncio.TimeoutError:
            return "done", "That's taking too long; I'll keep going and tell you when it's done."

    def _out(self, heard: str, words: str, *, awaiting: bool, want_audio: Any) -> dict[str, Any]:
        out: dict[str, Any] = {"heard": heard, "say": words, "awaiting": awaiting}
        if want_audio is not False and self.voice_ready() and words:
            try:
                out["audio"] = base64.b64encode(self.voice.speak(words)).decode("ascii")
            except Exception as exc:  # noqa: BLE001 -- the words still arrive
                _log.warning("speech failed (%s)", type(exc).__name__)
        return out


def _set(fut: asyncio.Future, value: str) -> None:
    if not fut.done():
        fut.set_result(value)
