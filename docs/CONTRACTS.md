# NOVA — M0 Interface Contracts

Authored by the lead. These are the seams between M0 modules. Implement to
these exactly; if a contract is wrong, say so and wait — do not quietly widen it.

Everything lives under `D:\NOVA`. Interpreter: `D:\NOVA\.venv\Scripts\python.exe`.
Model weights are **already on disk** (salvaged and byte-verified) — never
download at import time.

```
D:\NOVA\models\tts\kokoro-v1.0.onnx        D:\NOVA\models\vad\silero_vad.onnx
D:\NOVA\models\tts\voices-v1.0.bin         D:\NOVA\models\wake\*.onnx
D:\NOVA\cache\hf\hub\models--mobiuslabsgmbh--faster-whisper-large-v3-turbo
D:\NOVA\cache\hf\hub\models--Systran--faster-whisper-tiny.en
```

## Package layout (owned by T0.1)

```
nova/
  __init__.py        core constants, version
  config.py          load_config() -> Config
  bus.py             EventBus
  log.py             get_logger(name)
  audio/  stt/  tts/  skills/  security/  interfaces/
config/nova.yaml     the one config file
data/                runtime state (gitignored): audio_profile.json, gpu_profile.json, audit.log
tests/
```

## T0.1 — core

```python
def load_config(path: Path | None = None) -> Config   # dataclass tree, validated, typed
class EventBus:
    def subscribe(self, topic: str, handler: Callable[[Any], Awaitable[None]]) -> None
    async def publish(self, topic: str, payload: Any) -> None   # never raises into publisher
def get_logger(name: str) -> logging.Logger            # structured, writes data/nova.log
```

Rules: no module reads `os.environ` or hardcodes a path — everything comes from
`Config`. Unknown keys in `nova.yaml` are an error, not a shrug. A config option
that nothing reads must not exist (constitution: one source of truth).

## T0.2 — audio

```python
@dataclass(frozen=True)
class DeviceProfile:
    index: int; name: str; hostapi: str; samplerate: int
    verified: bool          # True only after real captured audio contained non-zero samples
    rms: float              # measured during probe

def probe_input_devices(force: bool = False) -> DeviceProfile   # caches data/audio_profile.json
class Capture:              # 16 kHz mono float32 frames
    def start(self) -> None; def read(self) -> np.ndarray; def stop(self) -> None
class Player:
    async def play(self, pcm: np.ndarray, samplerate: int) -> None   # cancellable
    def cancel(self) -> None
```

**The lying-endpoint trap (measured on this machine, correcting prior art):**
Windows exposes the same physical mic several times, and at least one endpoint
opens successfully while returning digital zeros. A device that opens is NOT a
working device.

Prior art said the WASAPI endpoint was the liar. On this laptop it is **device 17
(Realtek Mic input) on WDM-KS**, returning exact `0.0` RMS on 3/3 trials — so the
rule is "probe empirically", never "distrust one named host API".

The subtler half, and the reason a zero-check alone is not enough: the WASAPI and
MME duplicates of the *same physical mic* are not lying. They return real signal
at ~1e-4 RMS, squashed by the Windows audio engine's AGC and noise suppression —
non-zero, so they pass any all-zeros test, and useless for speech recognition.
Picking one would give an assistant that appears to work and mishears everything.
Ranking by host-API tier and then RMS is what separates the true hardware endpoint
(WDM-KS idx 22, RMS 0.048) from its own suppressed duplicates, not the silence
floor.

So: enumerate every input device, record a real second from each, compute RMS,
reject exact-zero and near-dither buffers, rank survivors WDM-KS -> MME ->
DirectSound -> WASAPI with RMS as the tiebreak, and cache the winner to
`data/audio_profile.json` keyed by a fingerprint of the device list so a new
headset re-probes. `verified=True` only when real non-silent audio arrived.

Silence floor is calibrated, not guessed: float32 dither sits ~1e-7, the confirmed
lying endpoint measures exactly 0.0, and the smallest genuine capture on this box
is 6e-5. `1e-6` sits ~10x above dither and ~60x below the smallest real signal.

Several devices refuse to open at 16 kHz directly (WDM-KS reports "Invalid
device"), so open at the device's native rate and resample to 16 kHz mono float32.
`Player` must cancel within one callback block, not at end-of-buffer — barge-in
depends on it.

## T0.3 — stt

```python
@dataclass(frozen=True)
class GpuProfile:
    device: str; compute_type: str; verified: bool
    gpu_name: str; driver: str; ct2_version: str; dll_directories: list[str]

def probe_gpu(force: bool = False) -> GpuProfile      # caches data/gpu_profile.json
class Transcriber:
    def load(self) -> None; def unload(self) -> None
    def transcribe(self, pcm: np.ndarray) -> str      # 16 kHz mono float32 in
```

**Two settled hazards, do not re-derive:**
1. Windows ignores `PATH` for extension-module DLLs. Call `os.add_dll_directory()`
   on every `site-packages/nvidia/*/bin` **before** anything imports `ctranslate2`,
   and record them in `dll_directories`. Put nvidia wheel dirs ahead of torch's.
2. Trust no capability list: load a real model and run a real transcription per
   candidate, in order `float16 → int8_float16`, keep the first that returns,
   and set `verified=True` only after an inference completed. Cache keyed by
   (gpu name, driver, ct2 version).

   **int8 is refused on compute capability ≥ 12 -- and the reason has changed.**
   Prior art recorded int8 loading fine and then dying with
   `CUBLAS_STATUS_NOT_SUPPORTED` on the first encoder run. Measured on this
   machine (driver 610.62, cuBLAS 12.9.2, cuDNN 9.24, ct2 4.8.1, compute
   capability 12.0), **that failure does not reproduce**: `int8` and
   `int8_float16` both completed on tiny.en and on large-v3-turbo. The refusal
   is retained as a deliberate, documented choice, not because int8 currently
   crashes -- `float16` is verified, costs ~2.4 GB of an 8 GB card, and int8
   leaves nothing on the table worth a silent mid-conversation crash if a
   driver update reintroduces the fault. Revisit only if VRAM becomes the
   binding constraint.

   Residual asymmetry, recorded rather than assumed away: the probe verifies
   with `tiny.en` for speed while the assistant runs `large-v3-turbo`, so a
   compute type that works small and fails large would pass. `GpuProfile` must
   record which model the verification actually ran against.

## T0.4 — tts

```python
class Synthesizer:
    def load(self) -> None; def unload(self) -> None
    def say(self, text: str) -> tuple[np.ndarray, int]   # (pcm, samplerate)
```
Kokoro ONNX via onnxruntime, weights from `models/tts/`. If Kokoro fails, fall
back to Windows SAPI and **log a WARNING naming Kokoro** — NOVA must never go
silently mute.

## T0.5 — security + skills

Amended after nova-security's threat model (`docs/THREAT_MODEL.md`). The changes
below are requirements, not suggestions — each closes a hole the retired build
actually shipped.

```python
class Tier(Enum): READ = "read"; ACT = "act"; CONFIRM = "confirm"

@skill(name=..., tier=Tier..., examples=[...], trusts_input=False)
async def handler(ctx: SkillCtx, **params) -> Reply: ...

class Ladder:
    async def authorize(self, skill: SkillSpec, params: dict) -> Decision
class AuditLog:
    def record(self, event: dict) -> None   # append-only, hash-chained; RAISES on write failure
    def verify(self) -> ChainStatus         # walks the chain, reports the first break
class Egress:                               # THE ONLY MODULE THAT MAY REACH THE NETWORK
    def fetch(self, url: str) -> bytes                              # allowlisted URLs
    def release_for_process(self, purpose: str, payload: str) -> str  # text crossing to a spawned process
```

Registry is import-time discovery of `nova/skills/*.py`. No hand-wiring: the
router reads `examples`, the ladder reads `tier`, and the injection suite
enumerates `trusts_input=True` skills and blocks any that lacks hostile-content
tests. Duplicate skill names raise loudly; they never silently overwrite. M0
ships two skills: `time` (READ) and `open_app` (ACT).

### Redaction config

`SecurityConfig.redact_terms: list[str]`, empty by default — Manav's own private
terms, on top of the shape-based rules. Redaction matches the *shape* of a home
directory (`C:\Users\<x>`, `/home/<x>`, both slash styles) rather than needing to
know the username, because nothing but `Config` may read the environment. One
shared implementation in `nova/security/redact.py`; no per-feature copies.

### Process-spawn allowlist (for the rescoped AST test)

Three entries, each earning it, plus one reserved:
- `nova/security/egress.py` — transport imports, and nothing else may have them.
- `nova/stt/gpu.py` — spawns `nvidia-smi` with fixed argv, read-only, no external
  input. Verified present at `gpu.py:61` before the test was written; it predates
  the rule and would otherwise have broken T0.3 the moment the test landed.
- `nova/skills/open_app.py` — realpath-validated launch only.
- Reserved, not pre-approved: M4's Claude CLI adapter, added when the file exists.

### Audit failure means deny

`record()` raises on write failure, and `Ladder.authorize()` denies any ACT or
CONFIRM action whose audit write failed. The rule inherited from the old build —
*"we could not log that this left"* must mean *"it did not leave"* — extends to
actions, not just egress. Silent proceed-without-a-record is forbidden.

### CONFIRM fails closed, and audibly

`authorize()` on a CONFIRM-tier action with no confirmation channel registered
returns an **audited** `Decision.deny` with reason `no_confirm_channel`. Test this
in M0 even though M0 ships no CONFIRM skills, so that M1 only has to attach a
channel rather than discover a silent bypass. The retired build's real failure
was not a missing dialog — it had two — but that CONFIRM defaulted to
unapprovable for most of its life *with no loud signal*, and was never once
verified against a human clicking anything.

### `open_app` is arbitrary code execution wearing a hat

`os.startfile("http://evil.example")` opens the default browser and fetches the
URL **through the shell**, so `Egress` never sees it. UNC paths
(`\\host\share\payload.exe`) do the same across the network. Therefore:
closed allowlist of known applications in config; reject URL-shaped and UNC-shaped
input *before* touching any Windows API; `realpath`-verify the resolved target
against the allowed-directory set **at call time**, not at load time (junctions
move); `Popen([resolved_path], shell=False)` with an argument list, never a bare
string, never `shell=True`; unknown app name is a refusal, not a best guess.

### The no-network test bans spawning, not just sockets

The old AST test walked `nova/` for socket-family imports — and its `TRANSPORTS`
set never included `subprocess`, while **seven** files under `nova/` imported it.
The promise stayed green over a hole you could drive a process through. So the
test is rescoped and renamed to enforce what it actually claims: **no file may
open a socket, or spawn a process capable of opening one.** Banned outside a short
named allowlist: `subprocess`, `os.system`, `os.popen`, `os.exec*`, `os.spawn*`,
`os.startfile`, `asyncio.create_subprocess_*`, plus the socket family.

### `Untrusted` is a type, not a label

A `str` subclass is a naming convention that the first `f"{x}"` defeats. To be
structural: **not** a `str` subclass, so passing it where `str` is annotated fails
loudly instead of duck-typing through; no `__str__`/`__format__` returning the raw
content; exactly one named `reveal()`; and an AST test asserting the router and the
skill-parameter binder never call it. This makes the *lazy* bypass fail loudly —
the injection test suite is still what proves the *deliberate* one doesn't exist.

## Cross-cutting rules

- Type hints everywhere; `from __future__ import annotations`.
- Every module degrades loudly: a missing piece logs a WARNING **naming it**.
- No absolute clock times in fixtures (constitution).
- Paths: spaces, backslashes and long paths are normal here — test for them.
- Hardware claims need evidence. "It should work" is not a result.

---

# M1 Interface Contracts

Written **after** these modules landed, which is my error — I assigned M1 work by
message and never wrote the contract, so nova-tester had code but no spec to test
against. Recorded here so the promises are testable rather than inferred from
implementation. Where this disagrees with the code, the contract is the promise
and the code is wrong.

## T1.2 — governor (`nova/governor.py`)

```python
TOPIC_TURN_START = "turn.start"   # published by app.py when a turn begins
TOPIC_TURN_END   = "turn.end"     # ...and when it ends, including on every early return

class Governor:
    def __init__(self, bus: EventBus, config: GovernorConfig | None = None)
    def register(self, name, component: LoadableComponent, *, typical_rss_mb: float) -> None
    def current_rss_mb(self) -> float
    def snapshot(self) -> GovernorSnapshot
    def check_once(self) -> None        # public + synchronous: drivable without a poll loop
```

Promises:
- **Never unloads mid-turn.** Between `turn.start` and `turn.end` the governor
  withholds unloading, no matter what RSS says.
- **Never calls `load()`.** Components lazy-load themselves; the governor only
  ever drives `unload()`. It reduces; it does not summon.
- `typical_rss_mb` is **required, no default** — it affects unload *order* only;
  the decide-to-unload signal is always live `psutil`. A default would be an
  invented number, and invented numbers are what this project keeps deleting.
- An unreachable ceiling logs a WARNING and returns. It must not loop, thrash,
  or crash.
- The ceiling (900 MB) sits **above** Whisper's measured host-RSS floor
  (~741 MB), deliberately. A ceiling below the floor makes the governor fight
  physics: unload, reload, unload, paying latency forever and freeing nothing.
  Its honest job is *shed Kokoro when both are idle*, not *return to cold idle*.
  See `docs/GOVERNOR_MEASUREMENTS.md` and ADR 0006.

## T1.1 — wake word + VAD (`nova/audio/wake.py`, `nova/audio/vad.py`)

```python
TOPIC_WAKE_START = "wake.start"   # payload carries preroll PCM
TOPIC_WAKE_END   = "wake.end"     # mode: "vad_end" | "arm_timeout"

class Vad:
    def load(self); def reset(self); def process(self, frame) -> float   # 480-sample frames
```

Promises:
- **Wake and push-to-talk are interchangeable arming sources.** The turn loop
  must not care which fired. That interchangeability is the design goal — the
  wake word is a second door into the same room, not a parallel pipeline.
- **Opens no audio device.** `process(frame)` is fed by whoever owns the single
  `Capture`. Two components competing for the mic is a bug we get to not have.
- **Never boots deaf-and-silent.** A missing model file → WARNING naming the
  exact file, `backend_available=False`, `process()` a safe no-op. A config
  pointing at a nonexistent `hey_nova.onnx` must never look healthy.
- **`hey_nova.onnx` is a pure file drop-in.** Embedding count is read from the
  model's own input shape at load time, never hardcoded.
- **Privacy:** the ring buffer is the one sanctioned exception to the hotkey's
  zero-buffering rule — bounded, never grows, never logged, never written to
  disk. Raw PCM rides only on `wake.start`, where downstream legitimately needs
  it.

## T1.3 — confirm channel (`nova/security/confirm.py`)

`ConfirmChannel` is a `Protocol` with `async request(skill, params) -> bool`, so
a tray/dialog channel drops in with zero ladder changes.

Three causes, three audited reasons, all denying:

| Cause | Reason |
|---|---|
| channel returns `False` | `confirm_declined` |
| channel raises `ConfirmTimeout` | `confirm_timed_out` |
| channel raises anything else | `confirm_channel_error` |

An audit log recording that a human declined, when no human was ever consulted,
is worse than one that says nothing — because it will be believed.

The prompt shows the **actual post-redaction payload**, recursively through
dicts and lists. Consent is to a specific disclosure, not to a category.

## T1.7 — the duplex gate (finding F26), `nova/audio/duplex.py`

**This is a precondition for wiring `wake.py` into `app.py`, not a follow-up.**
The retired build hit the self-echo loop, documented it as *"a loop, not a
glitch"*, and shipped `aec.py` for it. This tree has no equivalent and no
coordination point at all — `Player.play()` and `WakeWord.process()` do not know
about each other. It is masked today only because push-to-talk stops `Capture`
before `_speak()` runs, which is a side effect of PTT's shape rather than a
control. Wiring the wake word in without one reproduces a solved bug on purpose.

```python
class DuplexGate:
    def mic_open(self) -> bool          # False while NOVA is speaking (+ hangover)
    def on_speak_start(self) -> None    # bus: TOPIC_SPEAK_START
    def on_speak_end(self) -> None      # bus: TOPIC_SPEAK_END, starts the hangover
```

Requirements:

- **Half-duplex is sufficient for M1.** Mute while `Player` is active, with a
  short hangover after. Full acoustic echo cancellation is explicitly *not*
  required to ship safely — neither full-duplex backend has a reliable Windows
  wheel, and the prior build reached the same conclusion. Do not build AEC.
- **Bus-driven, not direct calls.** `TOPIC_SPEAK_START`/`TOPIC_SPEAK_END` already
  exist and are already published by `NovaApp._speak()` for the tray. Subscribe;
  do not reach into `Player`. Two components already coordinate through those
  topics — a third is free.
- **`WakeWord.process()` consults the gate and returns early when the mic is
  shut.** It must not score audio captured while NOVA is talking. That is the
  entire point: the detector getting a clean shot at NOVA's own reply is the
  loop.
- **The hangover is a guess and must say so.** The retired build used 180 ms,
  reasoning about speaker decay plus WASAPI output buffering, and never measured
  it. Pick a number, put the reasoning next to it, make it config-driven, and
  label it as untuned rather than as a finding.
- **Fail closed.** If gate state is unknown or the subscription never arrived,
  the mic is treated as shut for wake purposes. A gate that fails open is the
  same as no gate, and it would fail open exactly when something has gone wrong.
- **Push-to-talk is unaffected.** PTT already closes `Capture` around speech and
  is the deliberate barge-in path (`Ctrl+Alt+Space` cancels playback inline).
  The gate must not break it.

Explicitly **not** in scope here, and tracked separately: the arming-source
signal and tier cap (F28) are Manav's decision, and the threshold floor (F27)
needs the false-accept measurement now running. This contract is the part that
holds regardless of how either resolves.
