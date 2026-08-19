"""Human-runnable tray icon probe -- the thing an agent cannot verify.

    D:\\NOVA\\.venv\\Scripts\\python.exe scripts\\probe_tray.py

Everything this module's automated report can prove synthetically (icon
handles get created, Shell_NotifyIcon calls don't raise, a simulated
WM_COMMAND reaches the quit handler) still leaves one question open: does a
human looking at the actual Windows notification area see a legible icon
that changes colour, and can they right-click it and click Quit? That is
what this script is for.

What to check, in order:
  1. Look at the notification area (bottom-right of the taskbar, click the
     "^" overflow arrow if you don't see it immediately -- Windows hides new
     tray icons there by default until you drag NOVA's onto the visible bar).
  2. Confirm the icon is a solid grey square, tooltip "NOVA — idle".
  3. Watch it cycle: green (listening) -> amber (thinking) -> blue
     (speaking) -> grey (idle), ~3s each, printed to this console as it
     happens so you can correlate.
  4. Right-click it. You should see a greyed-out "NOVA — <state>" label, a
     separator, and "Quit".
  5. Click Quit. This script should print "tray requested stop" and exit
     within a couple seconds -- if it hangs here, that's a real bug, not a
     timing fluke (the whole design point of the daemon-thread message loop
     is that it always winds down).

Not part of the test suite: this one needs eyes and a mouse.
"""
from __future__ import annotations

import asyncio
import sys

from nova.interfaces.tray import Tray, TrayState


async def main() -> int:
    stop = asyncio.Event()

    def on_quit() -> None:
        print("tray requested stop (Quit was clicked)")
        stop.set()

    tray = Tray(on_quit=on_quit)
    tray.start(loop=asyncio.get_running_loop())

    if not tray.backend_available:
        print("Tray backend did NOT come up -- see the WARNING above for why.")
        print("Nothing to look at; this probe has nothing further to do.")
        return 1

    print("Tray icon is up. Look at the notification area now.")
    print("Cycling through all four states, ~3s each...")

    cycle = [TrayState.LISTENING, TrayState.THINKING, TrayState.SPEAKING, TrayState.IDLE]
    cycle_task = asyncio.ensure_future(_run_cycle(tray, cycle, stop))

    print("When you've seen the colours change, right-click the icon and choose Quit.")
    print("(Or press Ctrl+C here if you'd rather abort without exercising Quit.)")

    try:
        await stop.wait()
    except KeyboardInterrupt:
        print("Ctrl+C -- skipping the Quit-menu check.")
    finally:
        cycle_task.cancel()
        tray.stop()

    print("Did you see all four colours, and did the menu/Quit both work? "
          "If yes, record it in docs/EVIDENCE.md.")
    return 0


async def _run_cycle(tray: Tray, cycle: list[TrayState], stop: asyncio.Event) -> None:
    while not stop.is_set():
        for state in cycle:
            if stop.is_set():
                return
            print(f"  -> {state.value}")
            tray.set_state(state)
            try:
                await asyncio.wait_for(stop.wait(), timeout=3.0)
            except asyncio.TimeoutError:
                pass


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
