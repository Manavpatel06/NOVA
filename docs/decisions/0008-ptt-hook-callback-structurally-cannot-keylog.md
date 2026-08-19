# ADR 0008: The PTT hook callback is split so raw keystrokes structurally cannot leak, not just by policy

**Status:** decided, implemented in `nova/interfaces/hotkey.py` (`_extract_combo_event`, `_on_event`). Closes findings F9 and F10 in `docs/TASKS.md`, both now marked **FIXED** (`4da6851`, AST-guarded and tested by `ff1a87d`).

## Context

Push-to-talk needs a hotkey that fires regardless of which window has focus — the entire point of a global PTT combo, per `nova/interfaces/hotkey.py`'s own module docstring. On Windows that requires a system-wide low-level keyboard hook (`WH_KEYBOARD_LL`, via the `keyboard` package's `keyboard.hook()`), which delivers **every keystroke on the machine** to NOVA's callback — passwords, chat messages, card numbers, all of it — not just the `Ctrl+Alt+Space` combo. This is inherent to the mechanism and an accepted cost, not an oversight: there is no Windows API that grants "only tell me about these three keys" system-wide.

That makes the hook callback the single most sensitive function in the codebase. The obvious defense — a code comment saying "never log or buffer the raw event, only the combo keys matter" — is a rule a future contributor has to remember every time they touch this function, and the function was expected to grow more complex over time (tap-vs-hold disambiguation, latch timers, mode strings). A rule that has to be remembered inside a growing function is a rule that eventually gets forgotten, typically via something that looks harmless in the moment — a debug `print(event)` added "temporarily" while chasing an unrelated bug, or a field added to a state dict "just in case."

## Decision

Split the hook callback into two functions with a hard boundary between them:

1. **`_extract_combo_event(event)`** — the *only* place in the module that ever touches the raw `event` object, and it is deliberately tiny (about a dozen lines) so "never logs, never buffers" is verifiable by reading the whole function, not by auditing every future change against a policy. A non-combo key is identified and discarded on the very next line, unconditionally — never buffered even transiently, never logged at any level including DEBUG, and its name/scan code never leaves the function. On a match, it returns a minimal stripped tuple `(key, event_type, timestamp)` — never the `event` object itself.
2. **`_on_event(event)`** — the actual callback registered with `keyboard.hook()`. It delegates to `_extract_combo_event` in its first line and every state-machine branch below (ARMED/LATCHED transitions, latch timers, bus publishes) operates only on the stripped tuple. The raw `event` is out of scope past that handoff **structurally**, not by convention — Python's own variable scoping means the growing state-machine code has no name bound to the raw event to accidentally reach for.

`_on_event` is additionally wrapped in a broad `except Exception`, logging only the exception's *type name* — never its message, and never `event` — because either could echo keystroke-derived content back into `data/nova.log`, a file that later gets attached to a bug report. This closes a related but separate risk (F10): an uncaught exception inside a `WH_KEYBOARD_LL` hook callback can get the hook silently removed by Windows, killing push-to-talk with zero NOVA-side signal.

## Why

**Deny the capability structurally rather than police the misuse of it, because a rule you must remember is one you will eventually forget** — the same shape of argument as `docs/decisions/0005-untrusted-is-not-a-dataclass.md`'s move from "policy says don't unwrap `Untrusted` carelessly" to "the type doesn't implement the protocols that would let you." Here, the equivalent move is: don't ask every future edit to the state machine to remember not to log `event` — make it impossible for that code to reference `event` at all, by never handing it a name bound to it. The only function that still needs review scrutiny on every change is the small one whose entire job is exactly that boundary; everything downstream is free to grow in complexity (new modes, new timers, new bus topics) without becoming a new place a keylogging bug could appear.

## Consequences

- Any future extension to the hotkey state machine (a third mode, a new failsafe timer, additional bus metadata) is safe from this specific risk by construction — the review burden for "could this leak a keystroke" collapses to reviewing `_extract_combo_event` alone, which rarely needs to change once the combo-matching logic is right.
- This is a repeatable pattern worth applying the next time a capability needs privileged or broad access to sensitive raw input (a plausible future example: a broader system hook or clipboard monitor) — extract-and-minimize into one small, reviewable boundary function, and let everything else operate only on the minimized result.
