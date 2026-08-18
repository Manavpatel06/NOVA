# ADR 0003: The no-network AST test bans process spawn, not just transport imports

**Status:** decided, implemented in `tests/test_no_network_or_process_spawn.py`.

## Context

The constitution requires: "All network egress goes through ONE sanctioned module with an enforced allowlist, and a test proves no other file opens a socket." The retired build had exactly this test (`_OLD_ARCHIVE/tests/test_no_direct_network.py`): an AST walk banning imports of `socket`, `requests`, `urllib`, etc. outside a named allowlist.

`nova-security`'s threat model (`docs/THREAT_MODEL.md` §1.1–§2) found that test's own `TRANSPORTS` set never included `subprocess` — and the retired build had seven files importing it freely. From that test's point of view, `os.startfile("http://evil.example")` (opens the default browser, fetches the URL through the shell) and `subprocess.Popen(name, shell=True)` (admits shell metacharacters) are invisible: neither is a transport import, so the AST scan never sees them, even though both are network- or arbitrary-code-execution-shaped. The promise stayed green over a hole a process spawn could drive straight through it.

## Decision

The rescoped test (`tests/test_no_network_or_process_spawn.py`) enforces two separate, individually-justified, size-pinned allowlists:
- **Transport imports** (`socket`, `ssl`, `urllib`, `http`, `requests`, `httpx`, …) — exactly one file, `nova/security/egress.py`.
- **Process spawn** (`subprocess`, `os.system`, `os.popen`, `os.startfile`, `os.exec*`/`spawn*`, `asyncio.create_subprocess_*`) — exactly two files today (`nova/stt/gpu.py`, read-only `nvidia-smi` query with a literal argv; `nova/skills/open_app.py`, allowlisted launch with `realpath` re-validation), plus one reserved-but-not-pre-approved slot for M4's Claude CLI adapter.

Both allowlists assert their own size (`test_process_spawn_allowlist_is_exactly_two_named_files_today`, `test_transport_allowlist_is_exactly_one_named_file`) so a third file silently joining either one is a test failure that names the number, not a set that grows unnoticed.

## Why

Conflating the two allowlists would let a transport-only exemption (`egress.py`) spawn processes for free, or a process-spawn exemption spawn arbitrary network-capable subprocesses without the transport scrutiny — different trust boundaries deserve different scrutiny, so they're checked separately even though both trace back to the same constitutional promise ("no other file opens a socket"). The retired build's own comments already warned about exemptions nobody re-derives the need for at review time; pinning the allowlist size mechanically is cheaper than trusting review discipline to catch drift.

## Consequences

- `open_app`'s allowlisted launch (`docs/THREAT_MODEL.md` §1.1) is reviewed by the identical mechanism as the Claude CLI's future process boundary will be — one test, two justified call sites, not a one-off carve-out.
- M4's Claude CLI adapter is explicitly *not* pre-approved by this ADR — the allowlist entry is reserved in the test's docstring but the test itself only recognizes it once the file exists and earns its own scrutiny (`docs/SECURITY_SPEC.md` §3.2).
- Every allowlisted file also has a positive test proving it *actually uses* the exemption it holds (`test_allowlisted_transport_file_actually_uses_a_transport`, `test_allowlisted_process_spawn_files_actually_spawn_a_process`) — an unused exemption is exactly the kind of thing that should shrink, not sit there.
