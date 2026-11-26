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
import secrets
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
SPEECH_KEEP_S = 180.0    # pieces of a long reply's speech are kept this long
SPEECH_WAIT_S = 30.0
MAX_SPEECH_JOBS = 16
PIECE_MIN = 60           # merge shorter sentences into the next piece
PIECE_MAX = 200


def split_speech(text: str) -> list[str]:
    """Sentences, merged into pieces of a speakable size. The first piece is
    kept short so the first sound comes as soon as possible."""
    text = " ".join((text or "").split())
    if not text:
        return [""]
    sentences = [x for x in re.split(r"(?<=[.!?…])\s+", text) if x]
    pieces: list[str] = []
    buf = ""
    for sentence in sentences:
        while len(sentence) > PIECE_MAX:  # one very long sentence: break at a comma or space
            cut = max(sentence.rfind(", ", 0, PIECE_MAX), sentence.rfind(" ", 0, PIECE_MAX))
            cut = cut if cut > 0 else PIECE_MAX
            head, sentence = sentence[:cut + 1].strip(), sentence[cut + 1:].strip()
            if buf:
                pieces.append(buf)
                buf = ""
            pieces.append(head)
        limit = PIECE_MIN if not pieces else PIECE_MAX
        if buf and len(buf) + 1 + len(sentence) > limit:
            pieces.append(buf)
            buf = sentence
        else:
            buf = f"{buf} {sentence}".strip()
        if not pieces and len(buf) >= PIECE_MIN:  # the first piece goes out as soon as it's a sentence or two
            pieces.append(buf)
            buf = ""
    if buf:
        pieces.append(buf)
    return pieces


FOLLOW_UP_S = 8.0        # after a bare "Nova": the next sentence needs no name


class _Session:
    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self.events: asyncio.Queue = asyncio.Queue()
        self.answer: asyncio.Future | None = None
        self.task: asyncio.Task | None = None
        self.used = time.monotonic()
        self.follow_until = 0.0
        # conversation mode (ADR 0029): open until, when NOVA last spoke, the last few lines
        self.convo_until = 0.0
        self.replied_at = 0.0
        self.recent: list[tuple[str, str]] = []


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
    def __init__(self, assistant: Any, voice: Any | None = None, face: Any | None = None,
                 judge: Any | None = None) -> None:
        self.assistant = assistant
        self.voice = voice
        self.face = face   # nova.hub.face.Face: the hub screen's live NOVA
        self.judge = judge  # nova.hub.conversation.Judge: unclear sentences in a conversation
        self._sessions: dict[str, _Session] = {}
        self._speech: dict[str, dict[str, Any]] = {}
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
        t0 = time.monotonic()
        out = self._handle(body, t0)
        return self._timed(out, t0, show=isinstance(body, dict) and body.get("timing") is True)

    def _timed(self, out: dict[str, Any], t0: float, *, show: bool = False) -> dict[str, Any]:
        """Where the time went (2026-11-26, "voice input is delayed"): ms for
        speech-to-text, thinking and the first piece of voice, in the hub's
        log -- and in the reply when the caller asks ("timing": true). Never
        the words. Nothing for ignored speech."""
        if not isinstance(out, dict) or out.get("ignored"):
            return out
        ms = out.pop("ms", {})
        ms["total"] = int((time.monotonic() - t0) * 1000)
        ms["think"] = max(0, ms["total"] - ms.get("stt", 0) - ms.get("voice", 0))
        _log.info("turn: hear %d ms, think %d ms, voice %d ms (total %d ms)",
                  ms.get("stt", 0), ms["think"], ms.get("voice", 0), ms["total"])
        if show:
            out["ms"] = ms
        return out

    def _handle(self, body: Any, t0: float) -> dict[str, Any]:
        if not isinstance(body, dict):
            raise HubError("body must be a JSON object")
        sid = body.get("session")
        if not isinstance(sid, str) or not SESSION_RE.fullmatch(sid):
            raise HubError("session must be 8-40 letters, digits, - or _")
        self._face("listening", "the phone")
        heard = self._heard(body)
        stt_ms = int((time.monotonic() - t0) * 1000) if "audio" in body and "text" not in body else 0
        session = self._session(sid)
        if session.answer is not None and not session.answer.done():
            # this is the answer to the question NOVA just asked
            self._loop.call_soon_threadsafe(_set, session.answer, heard)
        elif session.task is not None and not session.task.done():
            return self._with_stt(stt_ms, self._out(heard, "Still working on the last one.", awaiting=False, want_audio=body.get("speak")))
        elif not heard:
            return self._with_stt(stt_ms, self._out("", "I didn't catch that.", awaiting=False, want_audio=body.get("speak")))
        else:
            self._face("thinking", "the phone", heard)
            fut = asyncio.run_coroutine_threadsafe(self._start(session, heard), self._loop)
            fut.result(5)
        kind, words = asyncio.run_coroutine_threadsafe(self._next(session), self._loop).result(REPLY_WAIT_S + 5)
        return self._with_stt(stt_ms, self._out(heard, words, awaiting=kind == "ask", want_audio=body.get("speak")))

    @staticmethod
    def _with_stt(stt_ms: int, out: dict[str, Any]) -> dict[str, Any]:
        if stt_ms and isinstance(out, dict) and not out.get("ignored"):
            out.setdefault("ms", {})["stt"] = stt_ms
        return out

    def hear(self, body: Any) -> dict[str, Any]:
        """One sentence from a device's always-open mic (the laptop through
        NOVA Link, or the hub's own). Acted on only when it starts with
        NOVA's name, answers NOVA's question, follows a bare "Nova", or comes
        in an open conversation and is judged to be for NOVA (ADR 0029).
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
        t0 = time.monotonic()
        heard = self._heard({"audio": body["audio"]})
        stt_ms = int((time.monotonic() - t0) * 1000)
        return self._timed(self._with_stt(stt_ms, self.hear_text(device, heard, want_audio=body.get("speak"))), t0)

    def hear_text(self, device: str, heard: str, *, want_audio: Any = True) -> dict[str, Any]:
        from nova.brain.address import strip_address
        from nova.hub.conversation import is_closer
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
            if not heard:
                return {"ignored": True}
            names = self._names()
            command = strip_address(heard, names)
            now = time.monotonic()
            if command is None and now < session.follow_until:
                command = heard
            elif command is None and now < session.convo_until:
                if is_closer(heard, names):
                    return self._close(session, heard, want_audio)
                if self._for_nova(session, heard, now):
                    command = heard
            session.follow_until = 0.0
            if command is None:
                return {"ignored": True}
            if command == "":
                session.follow_until = time.monotonic() + FOLLOW_UP_S
                return self._out("", "Yes?", awaiting=True, want_audio=want_audio)
            if session.convo_until and is_closer(command, names):
                return self._close(session, heard, want_audio)
            channel = _PageChannel(session, name=f"your {device}", armed_by=ArmedBy.VOICE)
            self._face("thinking", f"your {device}", command)
            asyncio.run_coroutine_threadsafe(self._start(session, command, channel), self._loop).result(5)
            heard = command
        kind, words = asyncio.run_coroutine_threadsafe(self._next(session), self._loop).result(REPLY_WAIT_S + 5)
        self._converse(session, heard, words)
        return self._out(heard, words, awaiting=kind == "ask", want_audio=want_audio)

    # --- conversation mode (ADR 0029) -----------------------------------------

    def _names(self) -> list[str]:
        cfg = getattr(self.assistant, "config", None)
        return list(cfg.wake.names) if cfg is not None else ["nova"]

    def _open_s(self) -> float:
        cfg = getattr(self.assistant, "config", None)
        return float(getattr(getattr(cfg, "hub", None), "conversation_s", 0.0) or 0.0)

    def _for_nova(self, session: _Session, heard: str, now: float) -> bool:
        """A sentence without the name, in an open conversation: for NOVA?
        Clear cases are decided here; unclear ones by the judge. If the judge
        can't be asked, it isn't for NOVA."""
        from nova.hub.conversation import quick_verdict

        verdict = quick_verdict(heard, now - session.replied_at)
        if verdict is None and self.judge is not None:
            try:
                verdict = self.judge.decide(heard, list(session.recent))
            except Exception as exc:  # noqa: BLE001 -- never break the mic over a judgement
                _log.warning("conversation judge failed (%s)", type(exc).__name__)
                verdict = None
        return verdict is True

    def _converse(self, session: _Session, heard: str, words: str) -> None:
        """NOVA just answered: the conversation stays open OPEN_S from now."""
        open_s = self._open_s()
        if open_s <= 0:
            return
        from nova.hub.conversation import RECENT

        session.recent = (session.recent + [("Manav", heard), ("NOVA", words)])[-RECENT:]
        session.replied_at = time.monotonic()
        session.convo_until = session.replied_at + open_s
        if self.face is not None and hasattr(self.face, "conversation"):
            try:
                self.face.conversation(open_s)
            except Exception:  # noqa: BLE001
                pass

    def _close(self, session: _Session, heard: str, want_audio: Any) -> dict[str, Any]:
        session.convo_until = session.replied_at = 0.0
        session.recent = []
        if self.face is not None and hasattr(self.face, "conversation"):
            try:
                self.face.conversation(0)
            except Exception:  # noqa: BLE001
                pass
        return self._out(heard, "Anytime.", awaiting=False, want_audio=want_audio)

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
            self._face("thinking", "a phone call", heard)
            asyncio.run_coroutine_threadsafe(self._start(session, heard, channel), self._loop).result(5)
        return self._within(session, wait_s)

    def phone_wait(self, call_sid: str, wait_s: float) -> "tuple[str | None, bool]":
        return self._within(self._session(f"phone-{call_sid}"), wait_s)

    def _face(self, mode: str, a: str = "", b: str = "") -> None:
        if self.face is None:
            return
        try:
            if mode == "listening":
                self.face.listening(a)
            elif mode == "thinking":
                self.face.thinking(a, b)
            elif mode == "speaking":
                self.face.speaking(a)
        except Exception:  # noqa: BLE001 -- the screen never breaks a conversation
            pass

    def _within(self, session: _Session, wait_s: float) -> "tuple[str | None, bool]":
        async def get():
            return await asyncio.wait_for(session.events.get(), wait_s)

        try:
            kind, words = asyncio.run_coroutine_threadsafe(get(), self._loop).result(wait_s + 2)
        except (asyncio.TimeoutError, TimeoutError):
            if session.task is None or session.task.done():
                return "", False
            return None, False
        self._face("speaking", words)
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
        """The reply. Long ones are spoken in pieces (2026-11-25): the first
        sentence is synthesized now and returned with the words; the rest are
        synthesized in the background while it plays, and fetched in order
        with more(). Short replies are one piece, exactly as before."""
        out: dict[str, Any] = {"heard": heard, "say": words, "awaiting": awaiting}
        self._face("speaking", words)
        if want_audio is not False and self.voice_ready() and words:
            parts = split_speech(words)
            try:
                tv = time.monotonic()
                out["audio"] = base64.b64encode(self.voice.speak(parts[0])).decode("ascii")
                out.setdefault("ms", {})["voice"] = int((time.monotonic() - tv) * 1000)
            except Exception as exc:  # noqa: BLE001 -- the words still arrive
                _log.warning("speech failed (%s)", type(exc).__name__)
                return out
            if len(parts) > 1:
                out["more"] = self._speech_job(parts[1:])
                out["parts"] = len(parts) - 1
        return out

    # --- the rest of a long reply's speech ------------------------------------

    def _speech_job(self, parts: list[str]) -> str:
        jid = secrets.token_hex(8)
        job = {"parts": [None] * len(parts), "cond": threading.Condition(), "at": time.monotonic()}
        with self._guard:
            now = time.monotonic()
            for k in [k for k, v in self._speech.items() if now - v["at"] > SPEECH_KEEP_S]:
                del self._speech[k]
            while len(self._speech) >= MAX_SPEECH_JOBS:
                del self._speech[min(self._speech, key=lambda k: self._speech[k]["at"])]
            self._speech[jid] = job

        def run() -> None:
            for i, text in enumerate(parts):
                try:
                    audio = self.voice.speak(text)
                except Exception as exc:  # noqa: BLE001
                    _log.warning("speech failed (%s)", type(exc).__name__)
                    audio = b""
                with job["cond"]:
                    job["parts"][i] = audio
                    job["cond"].notify_all()

        threading.Thread(target=run, name="speech", daemon=True).start()
        return jid

    def more(self, body: Any) -> dict[str, Any]:
        """{"id", "part"} -> that piece of a long reply's speech, waiting for
        it if it's still being synthesized."""
        if not isinstance(body, dict) or not isinstance(body.get("id"), str) \
                or not isinstance(body.get("part"), int) or isinstance(body.get("part"), bool):
            raise HubError('body must be {"id": "...", "part": 0}')
        with self._guard:
            job = self._speech.get(body["id"])
        part = body["part"]
        if job is None or not 0 <= part < len(job["parts"]):
            raise HubError("no such speech")
        with job["cond"]:
            job["cond"].wait_for(lambda: job["parts"][part] is not None, timeout=SPEECH_WAIT_S)
            audio = job["parts"][part]
        if audio is None:
            raise HubError("still speaking that; ask again")
        return {"audio": base64.b64encode(audio).decode("ascii") if audio else "",
                "last": part == len(job["parts"]) - 1}


def _set(fut: asyncio.Future, value: str) -> None:
    if not fut.done():
        fut.set_result(value)
