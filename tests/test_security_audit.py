"""Contract: docs/THREAT_MODEL.md §1.3/§4, docs/CONTRACTS.md T0.5 — AuditLog.

    def record(self, event: dict) -> None   # RAISES on write failure
    def verify(self) -> ChainStatus         # walks the chain, first break

Fail-closed and tamper-evident are the two non-negotiable properties. This
module's job is only to make a write failure loud (raise AuditWriteError);
Ladder.authorize() is what turns that into a deny — tested separately in
test_security_ladder.py.
"""
from __future__ import annotations

import json
import threading

import pytest

from nova.security.audit import (
    GENESIS_HASH,
    AuditLog,
    AuditWriteError,
    ChainStatus,
    verify_chain,
)


@pytest.fixture
def log(tmp_path) -> AuditLog:
    return AuditLog(path=tmp_path / "audit.log")


# ---------------------------------------------------------------------------
# Basic append + chain shape
# ---------------------------------------------------------------------------


def test_record_appends_a_line_and_verify_reports_ok(log):
    log.record({"kind": "test.event", "n": 1})
    log.record({"kind": "test.event", "n": 2})

    status = log.verify()
    assert status.ok is True
    assert status.entries == 2


def test_first_entrys_prev_hash_is_genesis(log, tmp_path):
    log.record({"kind": "test.event"})
    line = (tmp_path / "audit.log").read_text(encoding="utf-8").strip()
    entry = json.loads(line)
    assert entry["prev_hash"] == GENESIS_HASH
    assert entry["seq"] == 0


def test_sequence_numbers_increment(log, tmp_path):
    for i in range(5):
        log.record({"kind": "test.event", "i": i})
    lines = (tmp_path / "audit.log").read_text(encoding="utf-8").strip().splitlines()
    seqs = [json.loads(line)["seq"] for line in lines]
    assert seqs == [0, 1, 2, 3, 4]


def test_each_entrys_prev_hash_matches_the_previous_entrys_hash(log, tmp_path):
    log.record({"a": 1})
    log.record({"a": 2})
    lines = (tmp_path / "audit.log").read_text(encoding="utf-8").strip().splitlines()
    entries = [json.loads(line) for line in lines]
    assert entries[1]["prev_hash"] == entries[0]["hash"]


def test_verify_on_a_nonexistent_file_is_ok_and_empty(tmp_path):
    status = verify_chain(tmp_path / "does_not_exist.log")
    assert status == ChainStatus(ok=True, entries=0)


# ---------------------------------------------------------------------------
# Tamper evidence
# ---------------------------------------------------------------------------


def test_editing_a_past_entrys_content_breaks_the_chain_from_there(log, tmp_path):
    log.record({"kind": "a"})
    log.record({"kind": "b"})
    log.record({"kind": "c"})

    path = tmp_path / "audit.log"
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    entry = json.loads(lines[1])
    entry["event"]["kind"] = "TAMPERED"  # content changed, hash not recomputed
    lines[1] = json.dumps(entry, sort_keys=True, separators=(",", ":"), default=str)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    status = log.verify()
    assert status.ok is False
    assert status.broken_at == 1


def test_deleting_a_past_entry_breaks_the_chain(log, tmp_path):
    log.record({"kind": "a"})
    log.record({"kind": "b"})
    log.record({"kind": "c"})

    path = tmp_path / "audit.log"
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    del lines[1]  # remove the middle entry; seq/prev_hash both now mismatch
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    status = log.verify()
    assert status.ok is False


def test_a_clean_untouched_chain_of_many_entries_verifies_ok(log):
    for i in range(20):
        log.record({"kind": "test.event", "i": i})
    status = log.verify()
    assert status.ok is True
    assert status.entries == 20


# ---------------------------------------------------------------------------
# Fail closed: record() raises rather than swallowing a write failure
# ---------------------------------------------------------------------------


def test_record_raises_audit_write_error_when_the_path_is_unwritable(tmp_path):
    # Point the log path AT a directory — opening a directory for append
    # raises OSError, which record() must convert to AuditWriteError, not
    # swallow.
    unwritable = tmp_path / "audit_dir_not_a_file"
    unwritable.mkdir()
    log = AuditLog(path=unwritable)

    with pytest.raises(AuditWriteError):
        log.record({"kind": "test.event"})


def test_a_failed_record_does_not_corrupt_a_previously_good_chain(tmp_path):
    good_path = tmp_path / "audit.log"
    log = AuditLog(path=good_path)
    log.record({"kind": "a"})

    # Simulate a later failure by swapping the path to something unwritable,
    # then confirm the original file's chain is still intact.
    log._path = tmp_path / "audit_dir_not_a_file"
    log._path.mkdir()
    with pytest.raises(AuditWriteError):
        log.record({"kind": "b"})

    status = verify_chain(good_path)
    assert status.ok is True
    assert status.entries == 1


# ---------------------------------------------------------------------------
# Payload sanitization: the audit log must not become a second unredacted
# transcript store.
# ---------------------------------------------------------------------------


def test_secret_shaped_keys_are_redacted_in_the_stored_payload(log, tmp_path):
    log.record({"kind": "test.event", "password": "hunter2", "api_key": "sk-ant-abc123"})

    line = (tmp_path / "audit.log").read_text(encoding="utf-8").strip()
    assert "hunter2" not in line
    assert "sk-ant-abc123" not in line
    entry = json.loads(line)
    assert entry["event"]["password"] == "<redacted>"
    assert entry["event"]["api_key"] == "<redacted>"


def test_audio_shaped_keys_are_redacted():
    from nova.security.audit import _sanitize

    out = _sanitize({"pcm": [0.1, 0.2, 0.3], "audio_path": r"C:\NOVA\data\clip.wav", "text": "hello"})
    assert out["pcm"] == "<redacted>"
    assert out["audio_path"] == "<redacted>"
    assert out["text"] == "hello"


def test_long_strings_are_truncated_with_a_count():
    from nova.security.audit import _MAX_STRING_LEN, _sanitize

    long_value = "x" * (_MAX_STRING_LEN + 250)
    out = _sanitize({"note": long_value})
    assert len(out["note"]) < len(long_value)
    assert "truncated 250 chars" in out["note"]


def test_long_lists_are_capped():
    from nova.security.audit import _MAX_LIST_ITEMS, _sanitize

    out = _sanitize({"items": list(range(_MAX_LIST_ITEMS + 20))})
    assert len(out["items"]) == _MAX_LIST_ITEMS


def test_deeply_nested_payload_is_capped_not_a_stack_overflow():
    from nova.security.audit import _sanitize

    nested: dict = {"v": "bottom"}
    for _ in range(20):
        nested = {"child": nested}
    out = _sanitize(nested)  # must not raise (RecursionError) and must terminate
    assert out is not None


# ---------------------------------------------------------------------------
# Concurrency: the lock must make interleaved record() calls safe
# ---------------------------------------------------------------------------


def test_concurrent_record_calls_produce_a_gapless_valid_chain(log):
    n_threads = 8
    per_thread = 10

    def worker(i):
        for j in range(per_thread):
            log.record({"kind": "concurrent", "thread": i, "j": j})

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    status = log.verify()
    assert status.ok is True
    assert status.entries == n_threads * per_thread
