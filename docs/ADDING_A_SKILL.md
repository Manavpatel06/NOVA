# Adding a skill

`docs/ARCHITECTURE.md`'s central claim: a new capability should cost **one file plus one test file, no edits anywhere else**. This document shows the worked path using the two skills that actually exist (`nova/skills/time.py`, `nova/skills/open_app.py`) and then states, plainly, whether the claim holds — checked by reading `nova/skills/registry.py` and `nova/brain/router.py`, not assumed from the docstrings that describe them.

## The mechanism, in one paragraph

`nova/skills/__init__.py` calls `discover_skills()` at import time, which uses `pkgutil.iter_modules()` to import every `nova/skills/*.py` module. Each module's `@skill(...)`-decorated handler registers itself into a module-level dict as a side effect of being imported — nothing calls back into a central list. `nova/brain/router.py`'s `Router()` (with no arguments) reads that live registry via `all_skills()` and matches spoken text against every skill's declared `examples`. `nova/security/ladder.py`'s `Ladder.authorize()` reads `skill.tier` off the same `SkillSpec`. So: **create the file, and the router, the ladder, and the registry all pick it up automatically the next time `nova.skills` is imported.**

## Worked example: `time.py` (READ, zero parameters)

```python
# nova/skills/time.py
from nova.security.tiers import Tier
from nova.skills.registry import Reply, SkillCtx, skill

@skill(
    name="time",
    tier=Tier.READ,
    examples=["what time is it", "what's the time", "tell me the time", "current time"],
    trusts_input=False,
)
async def time_skill(ctx: SkillCtx) -> Reply:
    now = datetime.now().strftime("%I:%M %p").lstrip("0")
    return Reply(text=f"It's {now}.")
```

- **`tier=Tier.READ`** — no confirmation friction, but still audited on every call (`Ladder._allow_with_degrading_audit`); READ is the one tier where an audit-write failure degrades (logs a WARNING, proceeds) instead of denying, because READ is defined as free/reversible.
- **`examples`** — the *only* thing the router sees. There's no separate intent-pattern file. Write the phrases the way a person would actually say them; the router scores input against these with a blended token-Jaccard / character-similarity metric (see `nova/brain/router.py`'s module docstring for the calibration method).
- **`trusts_input=False`** — this skill never reads attacker-controlled content, so it may not import or call `nova.security.untrusted.reveal()`; an AST test (`tests/test_security_untrusted.py::test_no_trusts_input_false_skill_imports_or_calls_reveal`) enforces that mechanically, not by convention.
- **Zero parameters** — the handler's signature (minus `ctx`) has no other arguments, so the router never attempts to extract one.

## Worked example: `open_app.py` (ACT, one parameter, allowlist-validated)

```python
# nova/skills/open_app.py
@skill(
    name="open_app",
    tier=Tier.ACT,
    examples=["open notepad", "launch calculator", "open file explorer", ...],
    trusts_input=False,
)
async def open_app(ctx: SkillCtx, app: str) -> Reply:
    ...
```

- **`tier=Tier.ACT`** — has a side effect (launches a process), so an audit-write failure here is a *deny*, not a degrade (`Ladder._allow_with_required_audit`).
- **One parameter (`app: str`)** — `Router._extract_params()` (see `nova/brain/router.py`) finds this via `inspect.signature(spec.handler)`, derives "trigger words" as the first token of each of the skill's own `examples` ("open", "launch" — never hand-maintained), strips those and a stopword list from the input, and hands back whatever's left as `app` — *if* it's non-empty and no more than 5 tokens. Anything else asks for clarification instead of guessing. **A skill with more than one parameter always asks for clarification** — there's no schema to split unstructured text into multiple slots, and the router's own docstring says so explicitly rather than pretending to be smarter than it is. If your skill needs more than one piece of information from speech, plan for that limit now: either fold it into one free-text parameter your handler parses itself, or accept that M0's router will never fill it in for you.
- **The actual security work — closed allowlist, URL/UNC shape rejection, `realpath()` re-validation at call time, `Popen([...], shell=False)`** — lives entirely inside the handler, not in any shared framework code. That's appropriate: `open_app` is arbitrary-code-execution-shaped and earns its own scrutiny (see `docs/THREAT_MODEL.md` §1.1). It does **not** cost extra files — it's still one file.

## What the registry enforces automatically, so you don't have to remember it

Reading `nova/skills/registry.py`'s `skill()` decorator directly:
- **Duplicate names raise `SkillError` at import time**, never a silent overwrite.
- **Empty `examples` raises `SkillError` at import time.**
- **A `tier` that isn't a real `Tier` member raises `SkillError` at import time.**

Reading `tests/test_skills_registry.py` (not the registry module itself — this is a *test-enforced* mechanical rule, which is exactly where the constitution puts it):
- **Every skill with `trusts_input=True` must have a paired hostile-content test module**, or `test_every_trusts_input_true_skill_has_a_paired_injection_test_module` fails the build. This is what makes "one file plus one test" literal rather than aspirational for a skill that touches untrusted content: one implementation file, one paired injection-test file.

Reading `nova/security/untrusted.py` and the AST test in `tests/test_security_untrusted.py`:
- If your skill is `trusts_input=False` (the default — appropriate for anything that doesn't read email/calendar/web/document content), it structurally *cannot* import `nova.security.untrusted.reveal` without the AST test failing the build.

## How to actually add one

1. Create `nova/skills/<name>.py` with an `@skill(...)`-decorated async handler: `name`, `tier`, `examples` (phrases a person would say), `trusts_input` (almost always `False` unless the handler is deliberately built and tested to read untrusted content).
2. If the skill needs its own config (an allowlist, a threshold, an API scope — `open_app` needed `allowed_apps`/`allowed_app_dirs`), add the fields to the right dataclass in `nova/config.py` and to `config/nova.yaml`. This *is* a second file touched, and that's correct, not a violation of the one-file claim — see "Where the claim doesn't hold" below for why this is a different case.
3. Write the paired test file (`tests/test_skills_<name>.py`) — nova-tester's job per `CLAUDE.md`, not the implementing dev's.
4. If `trusts_input=True`, also write the hostile-content injection test module the registry-enumeration test requires, and make sure it asserts the target skill path actually ran (`CLAUDE.md` rule 2: a test that doesn't prove the real path executed proves nothing).
5. Run the suite. Nothing else needs editing — no router file, no ladder file, no central skill list.

## The honest verdict

**The claim holds for the case both M0 skills are examples of: a skill that only needs `SkillCtx.config` and `SkillCtx.ladder`, declares its own `tier`/`examples`/`trusts_input`, and needs no new shared config.** Verified by reading `nova/skills/registry.py`'s `discover_skills()` (plain `pkgutil.iter_modules` over the package directory — no hand-maintained list anywhere) and `nova/brain/router.py`'s `Router()` (reads `all_skills()` fresh, no separate pattern file). Adding `time.py` or `open_app.py` to an empty `nova/skills/` directory genuinely requires touching nothing else in `nova/`.

**Two honest exceptions, found by reading the code, not invented as caveats for their own sake:**

1. **A skill needing new config is a second file, by design, not a bug in the claim.** `nova/config.py`'s own rule ("a config option that nothing reads must not exist") means a skill can't quietly reach into `nova.yaml` for an untyped value — `open_app`'s `allowed_apps`/`allowed_app_dirs` required adding fields to `SecurityConfig`. This is the constitution's "one source of truth" rule doing its job, not a hole in the one-file claim — the claim is about *wiring* (router/ladder/registry discovery), not about *config surface*, and those are different promises.
2. **A skill needing more per-invocation context than `config` and `ladder` requires a core edit.** `SkillCtx` (`nova/skills/registry.py`) is a frozen dataclass with exactly two fields today. Its own docstring says it "grows as real skills need more (bus access, etc.) — add a field only once a skill actually reads it." No M0 skill has needed a third field yet, so this hasn't been tested in practice — but the moment one does (e.g. a skill that needs to publish to the `EventBus`, or hold a reference to the `Transcriber` for a multi-turn interaction), adding that field is an edit to `nova/skills/registry.py` itself, which is exactly the "edit anywhere else" the architecture doc's claim says shouldn't be necessary. This is worth tracking the first time it actually happens (M2's Skill SDK formalization, per `docs/ROADMAP.md`), rather than assuming today's two-field `SkillCtx` will hold forever.

Neither exception was hit by either M0 skill — both genuinely cost one file (plus config, for `open_app`) and one test file. The claim is real for the shape of skill M0 has built so far; it is not yet proven against a skill that needs more context than `config`+`ladder`, because none has been written.
