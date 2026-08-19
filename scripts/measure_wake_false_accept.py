"""False-accept curve for the wake-word detector (T1.6, closes F27 with data
or explains why it can't be closed yet).

    D:\\NOVA\\.venv\\Scripts\\python.exe scripts\\measure_wake_false_accept.py

F27: `WakeConfig.threshold` had no floor, and until acoustic arming is
tier-capped (F28) it is the entire access-control surface for hands-free
arming. nova-security's threat model and T1.5's config-bounds work both
declined to invent a floor from the three numbers available at the time
(0.997 true-positive, 0.00005 on one control phrase, zero detections across
~15s of noise) — those describe behavior *at the current default*, not the
false-accept curve across the range someone would actually lower the
threshold into. This script is the instrument that was missing: it builds a
real distribution instead of three points.

**What this measures and how**: Kokoro synthesizes each negative text
(several voices for the adversarial categories), each clip is fed through
the *real* production detection pipeline (`nova.audio.wake.WakeWord`, the
same ONNX sessions the running app uses) and the PEAK score across the
whole clip is recorded — not just whether it crossed some particular
threshold, so the same run answers the question for every candidate
threshold at once.

**Reaching into WakeWord's "private" methods is deliberate, not a
workaround**: `_run_detection_step`, `_warmup_locked`, `_push_to_ring`,
`_state_lock` are read/driven directly so this script can (a) capture every
per-step score rather than only a threshold-crossing boolean, and (b)
reset each clip to the same cold-boot-into-silence state `WakeWord.start()`
itself establishes via `_warmup_locked()` (see that method's own docstring
for the real, previously-found bug it fixes — a classifier window that
starts empty scores everything near-zero for the first ~2.3s regardless of
content). Every clip must start from that same warmed, silence-primed state
or later clips would be scored against contamination from the previous
clip's trailing embeddings. `tests/test_audio_wake.py` already reaches into
these same methods for equivalent reasons (monkeypatching
`_run_detection_step` directly), so this isn't a new kind of coupling to
the module, just a new user of the existing one.

**Two honesty constraints, both load-bearing for how much this result can
be trusted**:
  1. Every synthesized clip is Kokoro TTS, not a human voice, not a
     television, not a room with two people talking over each other. This
     is a real improvement over one control phrase, and it is still not the
     real world — the report this script produces says so explicitly, not
     just this docstring.
  2. A subset is run *acoustically* (played through the real speakers,
     captured through the real mic, exactly like the original T1.1
     measurement) and compared against the same clips scored directly
     (resampled in software, never touching a speaker). If the two diverge
     materially, the direct-fed curve is the weaker instrument and the
     report says so rather than quietly preferring the more convenient
     number.
"""

from __future__ import annotations

import asyncio
import json
import queue
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from nova import DATA_DIR
from nova.audio.capture import Capture
from nova.audio.player import Player
from nova.audio.resample import LinearResampler
from nova.audio.wake import WakeWord, _State
from nova.bus import EventBus
from nova.tts._paths import KOKORO_MODEL_PATH, KOKORO_VOICES_PATH
from nova.tts.synthesizer import DEFAULT_KOKORO_LANG

WAKE_SR = 16000
FRAME_MS = 30  # matches AudioConfig's default frame_ms -- process() steps
               # internally every ~80ms of accumulated audio regardless of
               # the size fed per call, so this only needs to be "realistic",
               # not exactly MEL_STEP_SAMPLES.
FRAME_SAMPLES = int(WAKE_SR * FRAME_MS / 1000)
TRAILING_SILENCE_S = 1.2  # appended after every clip: detection has real
                          # latency (the classifier's receptive field is
                          # ~760ms), so content at the very end of a clip
                          # needs audio *after* it to be fully absorbed into
                          # an embedding window before scoring stops.

# ---------------------------------------------------------------------------
# The text set. Categories match the lead's brief; phrases are mine.
# ---------------------------------------------------------------------------

TRUE_POSITIVE = ["hey jarvis"]

PHONETIC_NEAR_MISSES = [
    "hey jarvis",
    "hey travis",
    "hey charlotte",
    "hey darling",
    "hey darlings",
    "a jarvis",
    "hey jarvi",
    "okay jarvis",
    "hey jared",
    "hey arvid",
    "hey harvis",
    "hey carvis",
    "hey servus",
    "hey jazz",
    "they jarvis",
    "hey jarvis jarvis",
    "um hey jarvis",
]

OTHER_WAKE_WORDS = [
    "hey google",
    "ok google",
    "alexa",
    "hey siri",
    "ok computer",
    "computer",
    "hey cortana",
    "hey bixby",
]

HEY_SENTENCES = [
    "hey how's it going",
    "hey can you grab that for me",
    "hey wait a second",
    "hey did you see the game last night",
    "hey I'll be there in five minutes",
    "hey what time is it",
    "hey listen to this",
    "hey nice to meet you",
    "hey are you still there",
    "hey I have a question",
]

ORDINARY_SPEECH = [
    "turn on the lights",
    "what's the weather today",
    "can you set a timer for ten minutes",
    "I think we should order pizza tonight",
    "the meeting got moved to three o'clock",
    "did you finish reading that book",
    "the dog needs to go outside",
    "let's watch a movie later",
    "I'm heading to the store, need anything",
    "traffic was terrible this morning",
    "she said she'd call back tomorrow",
    "the printer is out of paper again",
    "can you believe how hot it's been",
    "we should really clean the garage this weekend",
    "his flight landed an hour early",
]

# Male + female voices, spanning Kokoro's full accent/pitch range as sold --
# not independently verified as representative of anything beyond "the
# voices this TTS engine ships with".
VOICES = ["af_heart", "af_bella", "af_nova", "am_adam", "am_michael", "am_onyx"]

# category -> (phrases, voices to run each phrase through)
PLAN: dict[str, tuple[list[str], list[str]]] = {
    "true_positive": (TRUE_POSITIVE, VOICES),
    "phonetic_near_miss": (PHONETIC_NEAR_MISSES, VOICES),
    "other_wake_word": (OTHER_WAKE_WORDS, VOICES[:3]),
    "hey_sentence": (HEY_SENTENCES, VOICES[:2]),
    "ordinary_speech": (ORDINARY_SPEECH, VOICES[:2]),
}

# Phrases that still contain the literal keyword "jarvis" (with filler,
# repetition, or a dropped final consonant) -- these are not genuine
# negatives. Scoring near the true positive is *correct* robustness
# behavior for the classifier, not evidence about the false-accept surface,
# so they're reported separately rather than folded into the curve, where
# they would silently make the classifier look far worse than it is.
DISGUISED_POSITIVES = {
    "hey jarvis", "a jarvis", "hey jarvi", "okay jarvis", "they jarvis",
    "hey jarvis jarvis", "um hey jarvis",
}

# The acoustic subset -- kept small on purpose (real speakers, real time).
# One voice (af_heart, matching the original T1.1 measurement) so results
# are directly comparable to that 0.997/0.00005 baseline. Prioritizes the
# genuinely dangerous direct-fed finding (see the report this script
# produces): "hey carvis"/"hey harvis"/"hey jazz" scored 0.83-0.99 direct-
# fed -- essentially tied with the true positive -- which is a materially
# more important thing to verify acoustically than the disguised positives
# this list originally included.
ACOUSTIC_SUBSET = [
    ("true_positive", "hey jarvis"),
    ("phonetic_near_miss", "hey carvis"),
    ("phonetic_near_miss", "hey harvis"),
    ("phonetic_near_miss", "hey jazz"),
    ("phonetic_near_miss", "hey travis"),
    ("phonetic_near_miss", "hey charlotte"),
    ("other_wake_word", "hey google"),
    ("other_wake_word", "alexa"),
    ("hey_sentence", "hey how's it going"),
    ("ordinary_speech", "turn on the lights"),
]


@dataclass
class Result:
    category: str
    text: str
    voice: str
    peak_score: float
    method: str  # "direct" | "acoustic"
    trial_scores: list[float] = field(default_factory=list)  # acoustic only: every repeat trial's peak, not just the max of them


# ---------------------------------------------------------------------------
# WakeWord driven directly, offline -- see module docstring for why reaching
# into these methods is deliberate.
# ---------------------------------------------------------------------------


def build_wake() -> WakeWord:
    # threshold=1.1 is unreachable (the classifier's score is bounded in
    # [0, 1]) so the detector never arms and keeps stepping through the
    # entire clip -- we want the true peak, not just "did it cross the
    # current default", so nothing may cut detection short mid-clip.
    wake = WakeWord(EventBus(), threshold=1.1)
    wake.start()
    if not wake.backend_available:
        raise RuntimeError("WakeWord backend failed to load -- cannot measure anything. Check the WARNING above.")
    return wake


def _reset_and_warm(wake: WakeWord) -> None:
    with wake._state_lock:
        wake._state = _State.IDLE
        wake._samples_since_infer = 0
        wake._embeddings.clear()
        wake._warmup_locked()  # re-primes the ring with silence + the embeddings window -- see module docstring


def score_clip(wake: WakeWord, pcm_16k: np.ndarray) -> float:
    """Feed one 16kHz mono float32 clip through `wake` frame-by-frame
    (matching real Capture.read() frame sizes) and return the peak score
    observed across the whole clip."""
    _reset_and_warm(wake)
    scores: list[float] = []
    original_step = wake._run_detection_step

    def _capturing_step() -> float:
        s = original_step()
        scores.append(s)
        return s

    wake._run_detection_step = _capturing_step  # type: ignore[method-assign]
    try:
        for start in range(0, len(pcm_16k), FRAME_SAMPLES):
            frame = pcm_16k[start : start + FRAME_SAMPLES]
            if len(frame) == 0:
                continue
            wake.process(frame)
    finally:
        wake._run_detection_step = original_step  # type: ignore[method-assign]
    return max(scores) if scores else 0.0


# ---------------------------------------------------------------------------
# Direct-fed measurement: Kokoro -> LinearResampler -> WakeWord. No speaker,
# no mic.
# ---------------------------------------------------------------------------


def measure_direct(kokoro, wake: WakeWord) -> list[Result]:
    results: list[Result] = []
    total = sum(len(phrases) * len(voices) for phrases, voices in PLAN.values())
    done = 0
    t0 = time.monotonic()
    for category, (phrases, voices) in PLAN.items():
        for text in phrases:
            for voice in voices:
                pcm24, sr = kokoro.create(text, voice=voice, lang=DEFAULT_KOKORO_LANG)
                pcm24 = np.asarray(pcm24, dtype=np.float32)
                pcm16 = LinearResampler(sr, WAKE_SR).process(pcm24)
                pad = np.zeros(int(WAKE_SR * TRAILING_SILENCE_S), dtype=np.float32)
                clip = np.concatenate([pcm16, pad])
                peak = score_clip(wake, clip)
                results.append(Result(category, text, voice, peak, "direct"))
                done += 1
        print(f"  [{done}/{total}] ...{category} done ({time.monotonic() - t0:.0f}s elapsed)")
    return results


# ---------------------------------------------------------------------------
# Acoustic measurement: real speakers, real mic, same detector, same clips.
# ---------------------------------------------------------------------------


async def _record_while_playing(pcm24: np.ndarray, sr: int, player: Player) -> np.ndarray:
    cap = Capture()
    cap.start()
    frames: list[np.ndarray] = []
    stop = threading.Event()

    def _reader() -> None:
        while not stop.is_set():
            try:
                frames.append(cap.read(timeout=0.2))
            except queue.Empty:
                continue

    reader = threading.Thread(target=_reader, daemon=True, name="wake-fa-measure-reader")
    reader.start()

    await asyncio.sleep(0.3)  # let the stream settle before playback starts
    await player.play(pcm24, sr)
    await asyncio.sleep(TRAILING_SILENCE_S)  # real trailing context, not just the padded silence

    stop.set()
    reader.join(timeout=2.0)
    cap.stop()

    return np.concatenate(frames) if frames else np.zeros(0, dtype=np.float32)


def _pick_stable_output_device() -> int:
    """A real, fixed physical output device -- NOT "Microsoft Sound Mapper -
    Output" and NOT a display/HDMI endpoint.

    Found the hard way: an early version of this script constructed a fresh
    `Player()` per acoustic trial, which re-probes the output device every
    call (`nova.audio.output.probe_output_device`). That probe correctly
    avoids this machine's OS-default HDMI/TV endpoint (the documented F18
    finding), but its fallback -- "Microsoft Sound Mapper - Output", an MME
    indirection layer that resolves to *whatever Windows' current default
    is* at stream-open time, not at probe time -- turned out to be exactly
    as unstable as the thing it was avoiding. Repeat trials of the identical
    clip on the identical text/voice swung from peak_score 0.98 to 0.015
    with captured RMS differing by ~10x between trials -- not real-world
    acoustic variance, an artifact of sometimes actually reaching the
    speakers and sometimes not. Switching to one fixed, concrete physical
    device (found here, verified once) and reusing one `Player` instance
    across every trial made repeat trials of the same clip land within a
    much tighter, plausible range. See the report this script produces for
    the concrete before/after numbers.
    """
    import sounddevice as sd

    from nova.audio.output import is_display_endpoint

    for i, d in enumerate(sd.query_devices()):
        name = d["name"]
        if d["max_output_channels"] <= 0:
            continue
        if is_display_endpoint(name):
            continue
        if "sound mapper" in name.lower():
            continue
        api = sd.query_hostapis()[d["hostapi"]]["name"]
        if api != "MME":  # matches nova.audio.player's own MME-first convention elsewhere in this codebase
            continue
        print(f"  Acoustic output device: [{i}] {name!r} ({api})")
        return i
    raise RuntimeError("No usable, non-Sound-Mapper, non-display output device found -- cannot run the acoustic subset")


ACOUSTIC_TRIALS_PER_CLIP = 3  # repeat trials, not one -- see _pick_stable_output_device's docstring for why a single acoustic sample isn't trustworthy on its own


async def measure_acoustic(kokoro, wake: WakeWord) -> list[Result]:
    results: list[Result] = []
    device = _pick_stable_output_device()
    player = Player(device=device)  # ONE instance, reused for every trial -- resolves/opens the stream once

    print(f"\n  Acoustic subset: {len(ACOUSTIC_SUBSET)} clips x {ACOUSTIC_TRIALS_PER_CLIP} trials each, real speakers + real mic.")
    print("  (Ambient conditions during this run are whatever's actually happening in the room --")
    print("   not controlled, and the report will say so.)")

    # Silent warm-up: the very first play() on a freshly opened stream showed
    # an anomalous amplitude spike relative to every call after it, even with
    # a fixed device -- burn that off here rather than let it contaminate
    # trial 1 of the first real clip.
    print("  (warm-up play, discarded)")
    await player.play(np.zeros(int(WAKE_SR * 0.3), dtype=np.float32), WAKE_SR)

    for i, (category, text) in enumerate(ACOUSTIC_SUBSET, 1):
        pcm24, sr = kokoro.create(text, voice="af_heart", lang=DEFAULT_KOKORO_LANG)
        pcm24 = np.asarray(pcm24, dtype=np.float32)
        trial_scores = []
        for trial in range(ACOUSTIC_TRIALS_PER_CLIP):
            print(f"  [{i}/{len(ACOUSTIC_SUBSET)}] playing: {text!r} (trial {trial + 1}/{ACOUSTIC_TRIALS_PER_CLIP}) ...")
            captured = await _record_while_playing(pcm24, sr, player)
            trial_scores.append(score_clip(wake, captured))
        peak = max(trial_scores)
        print(f"      -> trials: {[f'{s:.5f}' for s in trial_scores]}  peak={peak:.5f}")
        results.append(Result(category, text, "af_heart", peak, "acoustic", trial_scores=trial_scores))
    return results


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def print_curve(direct_results: list[Result]) -> None:
    tp = [r for r in direct_results if r.category == "true_positive"]
    disguised = [r for r in direct_results if r.category != "true_positive" and r.text in DISGUISED_POSITIVES]
    non_tp = [
        r for r in direct_results
        if r.category != "true_positive" and r.text not in DISGUISED_POSITIVES
    ]

    print("\n" + "=" * 78)
    print("SANITY CHECK -- disguised positives (variants/fillers still containing 'jarvis')")
    print("=" * 78)
    print(
        f"{len(disguised)} clips excluded from the false-accept curve below: these are not "
        f"genuine negatives (\"okay jarvis\", \"hey jarvi\", \"um hey jarvis\", etc. all still "
        f"say the actual wake keyword). Scoring near the true positive here is correct "
        f"robustness, not a leak -- reported separately so it can't quietly make the real "
        f"curve look worse than it is."
    )
    if disguised:
        lo, hi = min(r.peak_score for r in disguised), max(r.peak_score for r in disguised)
        print(f"  score range: {lo:.5f} - {hi:.5f} (all clustered near the true positive, as expected)")

    print("\n" + "=" * 78)
    print("FALSE-ACCEPT CURVE (direct-fed, genuine negatives only)")
    print("=" * 78)
    print(f"{len(non_tp)} negative clips across {len(set(r.category for r in non_tp))} categories "
          f"(disguised positives excluded).")
    print(f"True-positive clips (sanity check): {len(tp)}, "
          f"scores: {[f'{r.peak_score:.4f}' for r in tp]}")

    max_negative = max((r.peak_score for r in non_tp), default=0.0)
    worst = max(non_tp, key=lambda r: r.peak_score) if non_tp else None
    print(f"\nHighest-scoring genuine negative: {worst.peak_score:.5f}"
          f" ({worst.category}: {worst.text!r}, voice={worst.voice})" if worst else "n/a")

    print("\nCandidate threshold -> negatives that would fire (of {}):".format(len(non_tp)))
    for thr in [0.01, 0.02, 0.05, 0.08, 0.10, 0.15, 0.20, 0.25, 0.30, 0.35, 0.40, 0.45, 0.50, 0.55, 0.60, 0.70, 0.80, 0.90, 0.99]:
        fired = [r for r in non_tp if r.peak_score >= thr]
        marker = "  <- current default" if abs(thr - 0.55) < 1e-9 else ""
        print(f"  {thr:.2f}: {len(fired):3d} fire" + (f"  (worst: {max(f.peak_score for f in fired):.4f})" if fired else "") + marker)

    print(f"\nHeadroom above the highest genuine negative ({max_negative:.5f}): "
          f"a threshold set at {max_negative:.5f} would be the tightest floor "
          f"that still clears every genuine negative measured here -- note how close that "
          f"is to the true-positive scores above; see the report for what that means.")

    print("\nTop 15 highest-scoring genuine negatives (the ones that matter for picking a floor):")
    for r in sorted(non_tp, key=lambda r: -r.peak_score)[:15]:
        print(f"  {r.peak_score:.5f}  [{r.category:20s}] {r.text!r} (voice={r.voice})")


def print_acoustic_comparison(direct_results: list[Result], acoustic_results: list[Result]) -> None:
    print("\n" + "=" * 78)
    print("ACOUSTIC vs DIRECT-FED comparison (af_heart only)")
    print("=" * 78)
    print(
        f"Acoustic column shows every repeat trial, not just the peak -- the spread within "
        f"one phrase (same text, same voice, same speaker, same mic, seconds apart) is itself "
        f"the finding: see the report for what caused an early version of this script to show "
        f"trial-to-trial swings of 0.9+ before device selection was fixed (_pick_stable_output_device)."
    )
    direct_by_key = {(r.category, r.text): r.peak_score for r in direct_results if r.voice == "af_heart"}
    print(f"{'text':30s} {'direct':>10s}  {'acoustic trials'}")
    deltas = []
    for r in acoustic_results:
        d = direct_by_key.get((r.category, r.text))
        trials_str = ", ".join(f"{s:.4f}" for s in r.trial_scores) or f"{r.peak_score:.4f}"
        d_str = f"{d:10.5f}" if d is not None else f"{'n/a':>10s}"
        print(f"{r.text[:30]:30s} {d_str}  [{trials_str}]  (peak={r.peak_score:.4f})")
        if d is not None:
            deltas.append(r.peak_score - d)
    if deltas:
        print(f"\nmean delta (acoustic peak - direct): {np.mean(deltas):+.5f}   "
              f"max |delta|: {max(abs(d) for d in deltas):.5f}")


async def _main_async() -> None:
    # Everything below runs inside one asyncio loop, on purpose:
    # WakeWord.start() needs a running loop (only to satisfy its own
    # publish-a-wake-event seam -- this script's threshold=1.1 means that
    # seam is never actually exercised, but start() checks for a loop
    # before it even gets that far), and the acoustic subset needs a loop
    # for Player.play() regardless. One asyncio.run() for the whole script
    # is simpler than threading a loop reference through two call styles.
    print("Loading Kokoro and WakeWord...")
    from kokoro_onnx import Kokoro

    kokoro = Kokoro(str(KOKORO_MODEL_PATH), str(KOKORO_VOICES_PATH))
    wake = build_wake()
    print(f"WakeWord backend_available={wake.backend_available}, model={wake._model_path.name}")

    print("\nRunning direct-fed measurement (no speakers, no mic)...")
    direct_results = measure_direct(kokoro, wake)

    print_curve(direct_results)

    print("\nRunning acoustic subset (real speakers, real mic)...")
    acoustic_results = await measure_acoustic(kokoro, wake)
    print_acoustic_comparison(direct_results, acoustic_results)

    out_path = DATA_DIR / "wake_false_accept_measurement.json"
    payload = {
        "direct": [vars(r) for r in direct_results],
        "acoustic": [vars(r) for r in acoustic_results],
    }
    out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"\nRaw results written to {out_path}")

    wake.stop()


def main() -> None:
    asyncio.run(_main_async())


if __name__ == "__main__":
    main()
