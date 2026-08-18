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

**The WASAPI trap:** Windows exposes the same physical mic several times and the
WASAPI endpoint returns digital zeros. A device that opens successfully is NOT a
working device. Record a real second from every candidate and reject any whose
samples are all zero or whose RMS is indistinguishable from zero. Rank survivors
WDM-KS → MME → DirectSound → WASAPI. Cache the winner, keyed by the device list
so a new headset re-probes. `verified=True` means audio actually arrived.

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
2. `ctranslate2.get_supported_compute_types()` advertises int8 on this Blackwell
   card and then dies with `CUBLAS_STATUS_NOT_SUPPORTED` on first inference.
   Trust nothing: load a real model and run a real transcription per candidate,
   in order `float16 → int8_float16`, and keep the first that returns.
   `verified=True` only after an inference completed. Never select int8 on
   compute capability ≥ 12. Cache keyed by (gpu name, driver, ct2 version).

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

```python
class Tier(Enum): READ = "read"; ACT = "act"; CONFIRM = "confirm"

@skill(name=..., tier=Tier..., examples=[...], trusts_input=False)
async def handler(ctx: SkillCtx, **params) -> Reply: ...

class Ladder:
    async def authorize(self, skill: SkillSpec, params: dict) -> Decision
class AuditLog:
    def record(self, event: dict) -> None      # append-only, hash-chained
class Egress:                                   # THE ONLY MODULE THAT OPENS A SOCKET
    def fetch(self, url: str) -> bytes          # enforced allowlist, redaction on the way out
```

Registry is import-time discovery of `nova/skills/*.py`. No hand-wiring: the
router reads `examples`, the ladder reads `tier`, and the injection suite
enumerates `trusts_input=True` skills and blocks any that lacks hostile-content
tests. M0 ships two skills: `time` (READ) and `open_app` (ACT).

## Cross-cutting rules

- Type hints everywhere; `from __future__ import annotations`.
- Every module degrades loudly: a missing piece logs a WARNING **naming it**.
- No absolute clock times in fixtures (constitution).
- Paths: spaces, backslashes and long paths are normal here — test for them.
- Hardware claims need evidence. "It should work" is not a result.
