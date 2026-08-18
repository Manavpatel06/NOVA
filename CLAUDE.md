# NOVA — Team Constitution

This file binds the lead and every teammate. Read it before doing anything.

## What NOVA is

A voice-first, always-on personal assistant for Manav's Windows 11 laptop. Open-ended by design: the measure of the architecture is how cheap it is to add a new capability. NOVA hears, speaks, controls the system and apps, drives Claude Code to build software on command, and manages Manav's digital life — all locally.

## Non-negotiable constraints

- **Privacy**: data stays on this machine. All network egress goes through ONE sanctioned module with an enforced allowlist, and a test proves no other file opens a socket. Outbound text passes redaction (usernames, paths, secrets — case-insensitive, both slash styles).
- **Cost**: free / open-source only. $0 recurring.
- **RAM**: this is a 16 GB machine Manav also uses. Prefer small models and lazy loading, and **measure** before and after adding anything heavy.
  The original wording said "idle footprint near zero". That is measured to be unachievable in-process: CTranslate2 ships a caching allocator that pools freed memory by design, so once Whisper has transcribed once the process floors at ~735-770 MB (plus ~111-135 MB if Kokoro has run) for its lifetime. `unload()` frees VRAM but not this. It is a floor, not a leak -- confirmed by two independent cycle tests. **The real budget: ~60 MB cold, ~850-900 MB after any real use, <1.4 GB mid-turn.** Accepted deliberately by Manav on 2026-08-17 over moving models to a killable subprocess. See `docs/F16_MEMORY_RETENTION.md` and ADR 0006.
- **Windows 11 native**, Python 3.13, everything under `D:\NOVA` (C: is full). Long paths, spaces, and backslashes are normal — test for them.

## Security rules (nova-security enforces; everyone follows)

1. **Permission ladder**: every action NOVA can take is classified — READ (free), ACT (logged), CONFIRM (requires explicit human approval via a working UI channel, not a silent default-deny). The confirm path must be tested end-to-end or the feature doesn't ship.
2. **Untrusted input never reaches a tool.** Email bodies, calendar invites, web content, and document text are attacker-controlled. They may inform an answer; they may never select or parameterize an action. Every skill that touches outside content ships with prompt-injection tests that prove hostile content triggers zero tool calls — and those tests must assert the target path actually ran, otherwise they prove nothing.
3. **Filesystem writes** derived from stored manifests/config must re-validate destinations at write time with `realpath` on both sides — lexical path checks are bypassable with `..` and junctions.
4. **Sanitize rendered output** (markdown images/links can exfiltrate via URLs) with one shared implementation, not per-feature copies.

## Engineering rules

- **Tests are the contract.** New module = new tests, written by nova-tester, not by the implementing dev. The whole suite runs green before any task closes.
- **Time-independent tests.** Never pin fixtures to absolute clock times; a suite that goes red every afternoon is how real failures stop being noticed.
- **Confidence honesty.** If a router/intent match is below the confidence floor or crosses domains, NOVA asks a clarifying question — it never answers a low-confidence match as if certain.
- **Commit every module as it lands.** Work that exists only in a conversation does not exist.
- **One source of truth** per invariant — a test and a config file describing the same promise differently is a promise nobody can act on.

## Verification rules (nova-accuracy enforces)

Agents are markedly more reliable at building code than at describing what they did. Therefore:

- Every numeric claim in a report (test counts, pass/fail, latency, coverage) is re-run and confirmed by nova-accuracy before it is believed.
- "Fixed" requires revert evidence: reverting the named change must reproduce the failure, or the fix is credited to the wrong line.
- "It's the environment" is the most comfortable explanation available and gets the highest standard of proof — read the failing test's own docstring/source before accepting any framing.

## Team protocol

- The lead assigns work via the shared task list; teammates claim, plan, then implement. Non-trivial tasks get a plan reviewed by the lead first.
- Use `/code-review` on substantial diffs and `/debug` for structured debugging rather than ad-hoc flailing.
- Task completion order: tests green → security review → accuracy verification → docs updated → commit → mark complete.
- Grow this repo's `.claude/skills/` as patterns recur: when the team does the same multi-step thing twice, encode it as a skill.
