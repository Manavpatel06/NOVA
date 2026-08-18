"""Throwaway verification script for T0.1 (core). Not part of the test suite.

Run: D:\\NOVA\\.venv\\Scripts\\python.exe scripts\\verify_core.py
"""

from __future__ import annotations

import asyncio
import dataclasses

import nova
from nova.bus import EventBus
from nova.config import Config, ConfigError, load_config
from nova.log import get_logger

logger = get_logger("verify_core")


async def main() -> None:
    print(f"nova.__version__ = {nova.__version__}")
    print(f"nova.ROOT = {nova.ROOT}")
    print(f"nova.DEFAULT_CONFIG_PATH = {nova.DEFAULT_CONFIG_PATH}")
    print(f"nova.LOG_PATH = {nova.LOG_PATH}")

    # --- config: real load from config/nova.yaml -------------------------
    cfg = load_config()
    print("load_config() ->")
    print(f"  audio    = {cfg.audio}")
    print(f"  stt      = {cfg.stt}")
    print(f"  tts      = {cfg.tts}")
    print(f"  security = {cfg.security}")
    assert cfg.audio.samplerate == 16000
    assert cfg.stt.model == "large-v3-turbo"
    logger.info("config loaded ok: %s", dataclasses.asdict(cfg))

    # --- config: unknown key must be a hard error -------------------------
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as td:
        bad_path = Path(td) / "bad nova config.yaml"  # space, to test path handling
        bad_path.write_text("audio:\n  samplerate: 16000\n  bogus_key: 1\n", encoding="utf-8")
        try:
            load_config(bad_path)
        except ConfigError as exc:
            print(f"unknown-key rejection OK: {exc}")
        else:
            raise SystemExit("FAIL: expected ConfigError for unknown key")

        # --- config: wrong type must be a hard error -----------------------
        bad_type_path = Path(td) / "bad_type.yaml"
        bad_type_path.write_text("stt:\n  model: 5\n", encoding="utf-8")
        try:
            load_config(bad_type_path)
        except ConfigError as exc:
            print(f"wrong-type rejection OK: {exc}")
        else:
            raise SystemExit("FAIL: expected ConfigError for wrong type")

    # --- bus: publish/subscribe, failing subscriber must not break others -
    bus = EventBus()
    received: list[str] = []

    async def good_handler(payload: str) -> None:
        received.append(f"good:{payload}")

    async def bad_handler(payload: str) -> None:
        raise RuntimeError("boom - deliberate failure to prove isolation")

    async def second_good_handler(payload: str) -> None:
        received.append(f"second:{payload}")

    bus.subscribe("topic.test", good_handler)
    bus.subscribe("topic.test", bad_handler)
    bus.subscribe("topic.test", second_good_handler)

    await bus.publish("topic.test", "hello")
    print(f"bus received after publish (bad handler in the middle): {received}")
    assert received == ["good:hello", "second:hello"], received

    # publish to a topic with zero subscribers must not raise
    await bus.publish("topic.nobody-listens", {"x": 1})
    print("publish to topic with no subscribers: OK, no exception")

    logger.warning("this is a WARNING test line written to data/nova.log")
    print(f"log file exists: {nova.LOG_PATH.exists()}, size={nova.LOG_PATH.stat().st_size} bytes")

    print("ALL CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
