# NOVA — Architecture Proposal

Status: **proposed, awaiting approval**. Written by the lead before any code.

## The governing lesson

The retired build left 1582 green tests and a working assistant that had never
run on a microphone, a GPU, or Windows. Its own notes open with: *"NOTHING has
been tested on Windows, on a real GPU, or with a real microphone."* Logic was
proven; reality was not.

So this build inverts the order. **Hardware truth first, breadth second.**
Every milestone closes with a demonstration on this laptop, with Manav's own
mic and speakers, recorded in `docs/EVIDENCE.md` with the command that produced
it. A milestone with green tests and no hardware evidence is not closed.

## Shape of the system

One process. An event bus in the middle. Everything else plugs into it.

```
              ┌──────────────── nova.core (bus + state + governor) ───────────────┐
              │                                                                   │
  ears ───►  audio.capture ─► audio.wake ─► audio.vad ─► stt ─► brain.router ─────┤
              │                                                    │              │
              │                                        ┌───────────┴──────────┐   │
              │                                        │  security.ladder     │   │
              │                                        │  READ / ACT / CONFIRM│   │
              │                                        └───────────┬──────────┘   │
              │                                                    ▼              │
  voice ◄──  audio.playback ◄─ tts ◄─ brain.compose ◄────────  skills.registry     │
              │                                                    │              │
              └────────────── security.egress (the only socket) ───┴──────────────┘
```

### Layers

| Layer | Job | Chosen tech | Why |
|---|---|---|---|
| `nova.core` | bus, lifecycle, config, lazy model governor | stdlib asyncio | zero deps at the center |
| `nova.audio` | capture, wake, VAD, playback, duplex control | sounddevice, openWakeWord, silero-vad | proven wheels; weights already on disk |
| `nova.stt` | speech → text, GPU probe | faster-whisper `large-v3-turbo`, cuda/float16 | settled; int8 is broken on Blackwell |
| `nova.tts` | text → speech | Kokoro ONNX (default), Chatterbox (optional) | fast, small, local, natural |
| `nova.brain` | route intent, hold context, compose reply | rules first → local LLM fallback | rules are instant and free; LLM only when needed |
| `nova.skills` | every capability NOVA has | plugin registry | **the whole point — see below** |
| `nova.security` | permission ladder, egress guard, redaction, taint, audit | in-house | constitution requires it |
| `nova.interfaces` | hotkey, tray, CLI, phone | keyboard, pystray | PTT is a first-class input, not a fallback |

### The measure of the architecture: cost of a new skill

The constitution says the architecture is measured by how cheap a new capability
is. Concretely, adding one must be **one file plus one test file, no edits
anywhere else**:

```python
# nova/skills/timer.py
@skill(
    name="timer",
    tier=Tier.ACT,                       # READ / ACT / CONFIRM
    examples=["set a timer for 5 minutes", "wake me in an hour"],
    trusts_input=False,                  # declares it never routes on outside text
)
async def set_timer(ctx: SkillCtx, minutes: int) -> Reply:
    ...
```

Registration is by import-time discovery of `nova/skills/*.py`. The router picks
up `examples` automatically. The ladder reads `tier`. The injection test-suite
enumerates every skill with `trusts_input=True` and refuses to let it ship
without hostile-content tests. Nothing is wired by hand, so nothing can be
forgotten.

### Brain: three tiers, cheapest first

1. **Deterministic router** — patterns and examples from the skill registry.
   Microseconds, no RAM. Handles "open Chrome", "what time is it", "volume up".
2. **Local LLM** — a small GGUF model, lazy-loaded, unloaded when idle. Handles
   open language, paraphrase, multi-step intent, and natural replies.
3. **Claude Code arm** — for "build me an app that X". NOVA shells the local
   `claude` CLI headless, supervises the job, and reports back by voice.
   Always CONFIRM tier.

Below the confidence floor, or across domains, NOVA asks instead of guessing —
the constitution's confidence-honesty rule, enforced in the router, not in prose.

### Security, from day one, not bolted on

- **Ladder**: every skill carries a tier. CONFIRM routes through a real UI
  channel that is tested end-to-end before the feature ships.
- **Taint**: text from email, web, calendar, or documents is wrapped in
  `Untrusted[str]`. The router refuses to accept a routing decision derived from
  it — an injection cannot select or parameterize a tool call, structurally, not
  by prompt instruction.
- **Egress**: exactly one module opens a socket. A test walks the AST of every
  other file and fails the build if it imports a network library.
- **Redaction + output sanitizing**: one shared implementation each, no copies.

## Resource budget (16 GB machine Manav also uses)

| State | Target RSS | VRAM |
|---|---|---|
| Idle (listening only) | < 300 MB | 0 |
| Mid-turn (STT + TTS resident) | < 1.5 GB | ~2.4 GB |
| Brain loaded (LLM resident) | < 1.5 GB | ~5.5 GB |

These are targets to **measure**, not model. The prior build's memory table was
admittedly invented; ours gets numbers from `psutil` on this machine or it does
not go in the docs.
