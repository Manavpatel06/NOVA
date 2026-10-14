"""NOVA's main window (Manav, 2026-10-14).

NOVA is a normal windowed app: open it and this window appears and NOVA
starts listening; close it and NOVA shuts down and releases the microphone.

What the window shows
---------------------
* the NOVA mark and a live status ("Ready", "Listening", "Thinking",
  "Speaking", "Mic off"), echoed by an animated orb;
* the conversation -- only requests NOVA acted on and what it said back.
  Speech that was not addressed to NOVA never reaches this window;
* a text box (typing is the same as asking) and a mic on/off toggle.

Threading
---------
Tk runs on its own thread with its own mainloop, never on the asyncio loop.
The two sides talk only through:
  * bus handlers (asyncio side) -> a `queue.Queue` the Tk thread drains
    every frame;
  * Tk callbacks -> `nova.threadsafe.post_callback`/`post_coroutine` onto the
    asyncio loop.
Nothing on the Tk side calls app code directly, and this module never
imports tkinter at module import time, so `nova.app` stays importable on
machines without a display.

Security: display-only. Nothing received on the bus here can trigger an
action; the only actions the window originates are the user's own typing,
the mic toggle, and closing the window.
"""
from __future__ import annotations

import asyncio
import math
import queue
import threading
import time
from typing import Any, Awaitable, Callable

from nova import ASSETS_DIR
from nova.interfaces.tray import TOPIC_SPEAK_END, TOPIC_SPEAK_START
from nova.log import get_logger
from nova.threadsafe import post_callback, post_coroutine

_log = get_logger(__name__)

TOPIC_UI_HEARD = "ui.heard"   # payload: str -- a request NOVA is acting on
TOPIC_UI_SAID = "ui.said"     # payload: str -- what NOVA said back
TOPIC_UI_STATE = "ui.state"   # payload: str -- idle | listening | thinking | speaking | muted

# Palette taken from the icon (assets/nova.png).
INK = "#0D171C"
INK_2 = "#15232A"
INK_3 = "#1E3039"
LINE = "#2A3F48"
TEXT = "#E6EEF0"
TEXT_DIM = "#8FA3AB"
TEAL = "#4AC8B8"
STAR = "#FFD282"
STAR_HI = "#FFF0D2"
MUTED = "#5B6B72"

STATE_LABEL = {
    "idle": "Ready",
    "listening": "Listening…",
    "thinking": "Thinking…",
    "speaking": "Speaking",
    "muted": "Mic off",
}
STATE_COLOR = {
    "idle": TEAL,
    "listening": TEAL,
    "thinking": STAR,
    "speaking": STAR,
    "muted": MUTED,
}

HINT = "Say “Nova, …” — for example “Nova, what time is it?” — or type below."


class NovaWindow:
    def __init__(
        self,
        bus: Any,
        *,
        on_close: Callable[[], None],
        on_text: Callable[[str], Awaitable[None]],
        on_mic_toggle: Callable[[bool], Awaitable[None]],
    ) -> None:
        self._on_close = on_close
        self._on_text = on_text
        self._on_mic_toggle = on_mic_toggle
        self._inbox: "queue.Queue[tuple[str, Any]]" = queue.Queue()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._stop_requested = threading.Event()
        self._app_stopping = False
        self._close_posted = False
        self.available = False
        self.state = "idle"
        self.mic_on = True

        bus.subscribe(TOPIC_UI_HEARD, self._bus_heard)
        bus.subscribe(TOPIC_UI_SAID, self._bus_said)
        bus.subscribe(TOPIC_UI_STATE, self._bus_state)
        bus.subscribe(TOPIC_SPEAK_START, self._bus_speak_start)
        bus.subscribe(TOPIC_SPEAK_END, self._bus_speak_end)

    # -- bus side (asyncio thread) -----------------------------------------

    async def _bus_heard(self, text: Any) -> None:
        self._inbox.put(("you", str(text)))

    async def _bus_said(self, text: Any) -> None:
        self._inbox.put(("nova", str(text)))

    async def _bus_state(self, state: Any) -> None:
        self._inbox.put(("state", str(state)))

    async def _bus_speak_start(self, _payload: Any) -> None:
        self._inbox.put(("state", "speaking"))

    async def _bus_speak_end(self, _payload: Any) -> None:
        self._inbox.put(("state_after_speech", None))

    # -- lifecycle -----------------------------------------------------------

    def start(self, *, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self._thread = threading.Thread(target=self._run, name="nova-window", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._app_stopping = True
        self._stop_requested.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=3.0)

    # -- Tk side (window thread) ---------------------------------------------

    def _run(self) -> None:
        try:
            self._run_tk()
        finally:
            # NOVA only listens while its window is open (Manav's rule). If
            # the window could not open, or died for any reason other than
            # NOVA itself shutting down, shut NOVA down too -- never leave a
            # live microphone with no window to see or close it.
            if not self._app_stopping and not self._close_posted and self._loop is not None:
                _log.error("NOVA window is gone -- shutting NOVA down so the microphone is not left on unseen.")
                self._close_posted = True
                post_callback(self._loop, self._on_close)

    def _run_tk(self) -> None:
        try:
            import tkinter as tk
            from tkinter import font as tkfont
        except Exception as exc:  # noqa: BLE001
            _log.error("NOVA window unavailable: tkinter could not be imported (%s)", exc)
            return
        try:
            self._build(tk, tkfont)
        except Exception as exc:  # noqa: BLE001
            _log.error("NOVA window failed to open (%s)", exc)
            return
        self.available = True
        _log.info("NOVA window open")
        try:
            self._root.mainloop()
        finally:
            self.available = False
            try:
                self._root.destroy()
            except Exception:  # noqa: BLE001
                pass
            # Every Tk object must be released on THIS thread. Left for the
            # main thread's garbage collector, Tcl aborts the whole process
            # ("async handler deleted by the wrong thread") -- seen in a
            # headless render test on 2026-10-14.
            for name in ("_logo", "_log", "_entry", "_mic_btn", "_orb", "_dot", "_status", "_root", "_tk"):
                setattr(self, name, None)
            import gc

            gc.collect()

    def _build(self, tk: Any, tkfont: Any) -> None:
        root = tk.Tk()
        self._root = root
        self._tk = tk
        root.title("NOVA")
        root.geometry("440x680")
        root.minsize(360, 520)
        root.configure(bg=INK)
        try:
            root.iconbitmap(default=str(ASSETS_DIR / "nova.ico"))
        except Exception:  # noqa: BLE001 -- not fatal; Linux Tk can't read .ico
            pass
        try:
            # Windows: let the taskbar group NOVA as its own app, not "Python"
            import ctypes

            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Manav.NOVA")  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass

        families = set(tkfont.families(root))
        ui = next((f for f in ("Segoe UI Variable Text", "Segoe UI", "Inter", "Helvetica") if f in families), "TkDefaultFont")
        ui_semibold = next((f for f in ("Segoe UI Semibold", "Segoe UI Variable Display Semib") if f in families), ui)
        self._f_title = (ui_semibold, 17)
        self._f_status = (ui, 10)
        self._f_body = (ui, 11)
        self._f_small = (ui, 9)

        # header -----------------------------------------------------------
        header = tk.Frame(root, bg=INK)
        header.pack(fill="x", padx=18, pady=(16, 4))
        self._logo = None
        try:
            img = tk.PhotoImage(file=str(ASSETS_DIR / "nova.png"))
            factor = max(1, img.width() // 34)
            self._logo = img.subsample(factor, factor)
            tk.Label(header, image=self._logo, bg=INK).pack(side="left")
        except Exception:  # noqa: BLE001
            pass
        tk.Label(header, text="NOVA", fg=TEXT, bg=INK, font=self._f_title).pack(side="left", padx=(10, 0))
        status = tk.Frame(header, bg=INK)
        status.pack(side="right")
        self._dot = tk.Canvas(status, width=10, height=10, bg=INK, highlightthickness=0)
        self._dot.pack(side="left", padx=(0, 6))
        self._dot_id = self._dot.create_oval(1, 1, 9, 9, fill=TEAL, outline="")
        self._status = tk.Label(status, text="Ready", fg=TEXT_DIM, bg=INK, font=self._f_status)
        self._status.pack(side="left")

        # orb -----------------------------------------------------------------
        self._orb = tk.Canvas(root, height=190, bg=INK, highlightthickness=0)
        self._orb.pack(fill="x", padx=18, pady=(4, 6))

        # input row --------------------------------------------------------------
        row = tk.Frame(root, bg=INK)
        row.pack(side="bottom", fill="x", padx=18, pady=(0, 16))
        self._mic_btn = tk.Button(
            row, text="Mic on", command=self._toggle_mic, font=self._f_status, fg=INK, bg=TEAL,
            activebackground=TEAL, activeforeground=INK, relief="flat", bd=0, padx=12, pady=7, cursor="hand2",
        )
        self._mic_btn.pack(side="left")
        self._entry = tk.Entry(
            row, font=self._f_body, bg=INK_3, fg=TEXT, insertbackground=TEXT, relief="flat",
            highlightthickness=1, highlightbackground=LINE, highlightcolor=TEAL,
        )
        self._entry.pack(side="left", fill="x", expand=True, padx=8, ipady=7)
        self._entry.bind("<Return>", lambda _e: self._send())
        send = tk.Button(
            row, text="Send", command=self._send, font=self._f_status, fg=INK, bg=STAR,
            activebackground=STAR_HI, activeforeground=INK, relief="flat", bd=0, padx=14, pady=7, cursor="hand2",
        )
        send.pack(side="right")

        # conversation ----------------------------------------------------------
        conv = tk.Frame(root, bg=INK_2, highlightthickness=1, highlightbackground=LINE)
        conv.pack(fill="both", expand=True, padx=18, pady=(0, 10))
        self._log = tk.Text(
            conv, wrap="word", bg=INK_2, fg=TEXT, font=self._f_body, relief="flat",
            height=8, padx=12, pady=10, cursor="arrow", spacing1=2, spacing3=8, insertbackground=TEXT,
        )
        scroll = tk.Scrollbar(conv, command=self._log.yview, bg=INK_2, troughcolor=INK_2, relief="flat", bd=0, width=10)
        self._log.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self._log.pack(side="left", fill="both", expand=True)
        self._log.tag_configure("who_you", foreground=TEAL, font=self._f_small)
        self._log.tag_configure("who_nova", foreground=STAR, font=self._f_small)
        self._log.tag_configure("you", foreground=TEXT)
        self._log.tag_configure("nova", foreground=TEXT)
        self._log.tag_configure("hint", foreground=TEXT_DIM, font=self._f_small)
        self._log.insert("end", HINT + "\n", "hint")
        self._log.configure(state="disabled")

        root.protocol("WM_DELETE_WINDOW", self._close)
        root.bind("<Escape>", lambda _e: root.iconify())
        self._t0 = time.monotonic()
        root.after(30, self._tick)

    # -- Tk callbacks ----------------------------------------------------------

    def _close(self) -> None:
        try:
            self._root.withdraw()  # disappear instantly; the app shuts down behind it
        except Exception:  # noqa: BLE001
            pass
        if self._loop is not None and not self._close_posted:
            self._close_posted = True
            post_callback(self._loop, self._on_close)
        self._stop_requested.set()

    def _send(self) -> None:
        text = self._entry.get().strip()
        if not text or self._loop is None:
            return
        self._entry.delete(0, "end")
        post_coroutine(self._loop, self._on_text(text))

    def _toggle_mic(self) -> None:
        self.mic_on = not self.mic_on
        self._mic_btn.configure(
            text="Mic on" if self.mic_on else "Mic off",
            bg=TEAL if self.mic_on else MUTED,
            activebackground=TEAL if self.mic_on else MUTED,
        )
        if self._loop is not None:
            post_coroutine(self._loop, self._on_mic_toggle(self.mic_on))

    def _append(self, who: str, text: str) -> None:
        self._log.configure(state="normal")
        label = "You" if who == "you" else "NOVA"
        self._log.insert("end", label + "\n", "who_" + who)
        self._log.insert("end", text.strip() + "\n", who)
        self._log.configure(state="disabled")
        self._log.see("end")

    def _set_state(self, state: str) -> None:
        if state not in STATE_LABEL:
            state = "idle"
        self.state = state
        self._status.configure(text=STATE_LABEL[state])
        self._dot.itemconfigure(self._dot_id, fill=STATE_COLOR[state])

    def _tick(self) -> None:
        if self._stop_requested.is_set():
            self._root.quit()
            return
        try:
            while True:
                kind, payload = self._inbox.get_nowait()
                if kind in ("you", "nova"):
                    self._append(kind, payload)
                elif kind == "state":
                    self._set_state(payload)
                elif kind == "state_after_speech":
                    self._set_state("idle" if self.mic_on else "muted")
        except queue.Empty:
            pass
        except Exception as exc:  # noqa: BLE001
            _log.debug("window tick: %s", exc)
        self._draw_orb()
        # ~30 fps while something is happening, ~10 fps at rest
        self._root.after(33 if self.state not in ("idle", "muted") else 100, self._tick)

    def _draw_orb(self) -> None:
        c = self._orb
        c.delete("all")
        w = max(c.winfo_width(), 1)
        h = max(c.winfo_height(), 1)
        cx, cy = w / 2, h / 2
        t = time.monotonic() - self._t0
        state = self.state

        if state == "muted":
            arc_color, pulse, star_scale = MUTED, 0.0, 0.85
        elif state == "listening":
            arc_color, pulse, star_scale = TEAL, (math.sin(t * 6) + 1) / 2, 1.0
        elif state == "thinking":
            arc_color, pulse, star_scale = STAR, 0.5, 0.95
        elif state == "speaking":
            arc_color, pulse, star_scale = TEAL, (math.sin(t * 9) + 1) / 2, 1.0 + 0.08 * math.sin(t * 9)
        else:  # idle: slow breathing
            arc_color, pulse, star_scale = TEAL, (math.sin(t * 1.4) + 1) / 4, 0.95

        # listening arcs, left and right of the star
        for i, r in enumerate((54, 74)):
            rr = r + (6 * pulse if i == 1 else 3 * pulse)
            width = 5 if i == 0 else 4
            color = arc_color if (i == 0 or state in ("listening", "speaking")) else LINE
            for start in (-50, 130):
                c.create_arc(cx - rr, cy - rr, cx + rr, cy + rr, start=start, extent=100,
                             style="arc", outline=color, width=width)
        if state == "thinking":  # a sweeping arc while waiting on an answer
            rr = 90
            c.create_arc(cx - rr, cy - rr, cx + rr, cy + rr, start=(t * 240) % 360, extent=70,
                         style="arc", outline=STAR, width=3)

        # the nova: four-point star
        def star(radius_long: float, radius_short: float) -> list[float]:
            pts: list[float] = []
            for k in range(8):
                ang = math.pi / 4 * k
                rad = radius_long if k % 2 == 0 else radius_short
                pts += [cx + rad * math.sin(ang), cy - rad * math.cos(ang)]
            return pts

        s = 38 * star_scale
        fill = MUTED if state == "muted" else STAR
        c.create_polygon(star(s, s * 0.29), fill=fill, outline="")
        c.create_polygon(star(s * 0.5, s * 0.17), fill=STAR_HI if state != "muted" else TEXT_DIM, outline="")
