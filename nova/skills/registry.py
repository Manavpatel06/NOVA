"""The skill registry (docs/CONTRACTS.md T0.5).

Import-time discovery of `nova/skills/*.py` is the *only* path a skill's
tier, examples, and trusts_input reach anything else — no hand-wiring, no
central "list of skills" a person has to remember to update. A new capability
is one file plus one test; see nova/skills/__init__.py, which calls
discover_skills() on package import so `import nova.skills` alone is enough
to populate the registry.

Duplicate skill names are a hard error at import time, never a silent
overwrite (docs/THREAT_MODEL.md §4).
"""

from __future__ import annotations

import importlib
import inspect
import re
import pkgutil
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from nova.config import Config
from nova.log import get_logger
from nova.security.tiers import ArmedBy, Origin, Tier
from nova.security.untrusted import Untrusted

logger = get_logger("skills.registry")

if TYPE_CHECKING:
    # Deferred to break the registry.py <-> ladder.py import cycle: SkillCtx
    # holds a Ladder, Ladder.authorize() takes a SkillSpec -- this module
    # only ever uses Ladder in a type annotation (never isinstance-checked),
    # and `from __future__ import annotations` above makes every annotation
    # a string at runtime, so a real import here is never needed. Mirrors
    # the same move on ladder.py's side (nova-dev-sec, c1ea631).
    #
    # `Untrusted` is NOT deferred the same way, on purpose: layer 2 below
    # needs a real runtime isinstance() check, and a TYPE_CHECKING-only
    # import would make that check silently unreachable (NameError, or a
    # string-annotation no-op) rather than enforced. nova.security.untrusted
    # imports nothing from nova.skills or nova.security.ladder/registry (its
    # only in-package import, AuditLog, is itself TYPE_CHECKING-only), so
    # this import does not reintroduce the cycle the ladder.py move exists
    # to avoid.
    from nova.security.ladder import Ladder

Handler = Callable[..., Awaitable["Reply"]]

# F46/T2.0: what a skill hands back for a CONFIRM prompt to *show*, before
# the ladder has decided anything — see SkillSpec.resolve_for_confirm's own
# docstring below for the full contract (display-only, never a substitute
# for the handler's own call-time validation, and checked for having
# changed since it was shown before the handler ever runs).
ResolveForConfirm = Callable[["SkillCtx", dict[str, Any]], Awaitable[dict[str, Any]]]


class SkillError(ValueError):
    """Raised for a skill registration conflict (duplicate name, missing
    examples, wrong tier type) — always at import time, never at call time."""


# F28 built this as a small, deliberately narrow mapping (just
# "presence_required") and explicitly declined to expand it, since a
# general pass was scoped elsewhere at the time. F67 is that general pass:
# a denial's `reason` string is a machine-audit label first
# ("no_confirm_channel", "confirm_declined", ...) — spoken verbatim, it
# reads as a broken assistant rather than a deliberately limited one. That
# distinction matters more now that the wake word is live (F28/T2.4's
# `ceiling`): acoustic arming caps at READ tier by design, so Manav hits a
# CONFIRM-path refusal *regularly*, not as an edge case, and a refusal a
# human can't parse is exactly the path by which a working security
# control gets diagnosed as a bug and switched off. The one thing worse
# than a control Manav finds annoying is one he thinks is broken.
#
# Only the SPOKEN form is translated here. The audit log keeps `reason`
# exactly as `Ladder` produced it (`_deny_audited`/`_record` in
# nova/security/ladder.py never see this dict) — the log is for
# reconstructing what happened, the speech is for a human deciding what to
# do next. Same split nova-dev-4 drew for F46: audit keeps raw params, the
# CONFIRM prompt shows the resolved detail.
_FRIENDLY_DENIAL_TEXT: dict[str, str] = {
    "presence_required": "I can't do that unless you press the hotkey.",
    "no_confirm_channel": "I can't do that — I don't have a way to ask you to confirm it right now.",
    "confirm_declined": "You said no, so I didn't do that.",
    "confirm_timed_out": "I asked you to confirm, but didn't hear back in time, so I didn't do that.",
    "confirm_channel_error": "Something went wrong while I was asking you to confirm, so I didn't do that.",
    # The sharpest one: this fires when Ladder itself couldn't write the
    # audit record for what it was about to do, and refused rather than
    # act unrecorded (nova/security/ladder.py's Decision.deny("audit_write_
    # failed")). "I couldn't record that I did this, so I didn't do it" is
    # the actual, deliberate, defensible behavior underneath the label --
    # worth Manav understanding, not working around.
    "audit_write_failed": "I couldn't record that I was about to do that, so I didn't do it.",
}


def _spoken_denial_text(reason: str) -> str:
    """Turn a CONFIRM-denial machine reason into a sentence Manav can act
    on (F67). A reason with no mapping falls back to a generic-but-honest
    refusal rather than the raw snake_case string or a crash — and logs at
    WARNING so a newly-added denial reason gets *noticed* instead of
    quietly sounding like a bug forever."""
    text = _FRIENDLY_DENIAL_TEXT.get(reason)
    if text is None:
        logger.warning(
            "no spoken-text mapping for CONFIRM denial reason %r -- falling back to a "
            "generic refusal; add an entry to _FRIENDLY_DENIAL_TEXT in nova/skills/registry.py",
            reason,
        )
        return "I can't do that right now."
    return text


def _find_untrusted(value: Any, path: str) -> str | None:
    """Recursively search `value` for an `Untrusted` instance, returning a
    human-readable path to the first one found ("opts.app", "items[2]") or
    None if `value` is clean.

    Recurses through dict/list/tuple/set/frozenset — the container shapes a
    skill's `params` realistically take — rather than only checking the
    top-level value. A shallow check would pass `params={"app": Untrusted(...)}`
    straight through review while missing `params={"opts": {"app":
    Untrusted(...)}}` or an `Untrusted` sitting inside a list. That is not a
    hypothetical: the `Untrusted.asdict()` bypass (F12) was serious
    specifically because it was recursive through nested dataclasses/lists,
    so the same container shapes are worth checking here on the way in.

    F29 (nova-security): dict KEYS are checked too, not just values.
    `Untrusted` defines `__hash__`, so it is perfectly legal as a dict key —
    `params={"filters": {Untrusted(...): "x"}}` reached the handler with the
    key intact, unflagged and un-audited, because the first version of this
    function only ever recursed into `value.items()`'s second element. The
    top-level `params` dict is protected by Python itself (`**params`
    requires `str` keys), which is exactly why this was missed on first
    read and exactly why a *nested* dict has no such protection.
    """
    if isinstance(value, Untrusted):
        return path
    if isinstance(value, dict):
        for key, item in value.items():
            key_hit = _find_untrusted(key, f"{path}.<key>" if path else "<key>")
            if key_hit:
                return key_hit
            hit = _find_untrusted(item, f"{path}.{key}" if path else str(key))
            if hit:
                return hit
        return None
    if isinstance(value, (list, tuple, set, frozenset)):
        for index, item in enumerate(value):
            hit = _find_untrusted(item, f"{path}[{index}]")
            if hit:
                return hit
        return None
    return None


@dataclass(frozen=True)
class Reply:
    """What a skill hands back to the turn loop to speak/show to the user."""

    text: str
    success: bool = True


@dataclass(frozen=True)
class SkillCtx:
    """Per-invocation context threaded into every skill handler. Grows as
    real skills need more (bus access, etc.) — add a field only once a skill
    actually reads it, same rule as Config.

    `ladder` is required, not optional-with-a-None-default: a SkillCtx that
    lets invoke() silently skip authorization is exactly the unaudited-
    action gap docs/THREAT_MODEL.md closes. Every real caller constructs one
    Ladder (with a real AuditLog) at boot and threads it through here.

    `armed_by` (F28, T2.4's shared seam): how THIS turn was armed, honestly
    threaded from `nova/app.py`'s bus handlers (`_on_ptt_end` /
    `_on_wake_end`) — never guessed or reconstructed downstream. `invoke()`
    below turns it into `Ladder.authorize()`'s `ceiling=` keyword; see
    `nova/security/tiers.py`'s `ArmedBy` and `nova/security/ladder.py`'s
    module docstring for what it does and why it is not the floor's
    inverse. Defaults to `ArmedBy.PTT` — the unrestricted case — so every
    existing call site (every test fixture, every skill invoked outside
    the real turn loop) is unaffected unless it deliberately constructs a
    wake-armed context.

    `origin` (T2.4, docs/THREAT_MODEL_CLAUDE_FALLTHROUGH.md finding 2):
    who proposed this call, threaded from `nova/app.py`'s `run_turn()` —
    `Origin.ROUTER` for a local match, `Origin.CLAUDE` for a
    `nova.brain.claude_arm.ClaudeProposal` invoked after the router missed.
    `invoke()` below turns it into `authorize()`'s `floor=` keyword,
    computed the same way and right alongside `ceiling` above — see
    `nova/security/tiers.py`'s `Origin` and `ladder.py`'s module docstring
    for why a Claude-proposed action in a wake-armed turn carries both at
    once, and why the ceiling wins rather than either canceling the other.
    Defaults to `Origin.ROUTER` for the same backward-compatibility reason
    `armed_by` defaults to `ArmedBy.PTT`.
    """

    config: Config
    ladder: "Ladder"
    armed_by: ArmedBy = ArmedBy.PTT
    origin: Origin = Origin.ROUTER
    # 2026-10-15: the app's one nova.security.egress.Egress, for skills that
    # fetch from an allowlisted host (weather). None outside the real app.
    egress: Any = None


@dataclass(frozen=True)
class SkillSpec:
    name: str
    tier: Tier
    examples: tuple[str, ...]
    # T2.4 (docs/THREAT_MODEL_CLAUDE_FALLTHROUGH.md): a short, one-line
    # summary written for an external model reading it cold, deciding
    # between answering directly and proposing this skill -- NOT
    # `handler.__doc__`, checked and rejected deliberately, not by default.
    # The four real docstrings that existed when this field was added are
    # written for a developer maintaining the file: multi-paragraph, cross-
    # referencing "see module docstring," one ending mid-thought if
    # truncated to a single line. That's the right shape for its actual
    # audience and the wrong shape for Claude's system prompt. This is a
    # second necessary artifact for a second real audience, not a second
    # source of truth for the same fact -- the same reasoning `Reply.text`
    # (spoken to Manav) is deliberately not the log line describing the
    # same event. Required, enforced at import time exactly like `examples`
    # below (SkillError on empty) -- a skill Claude can't meaningfully
    # choose between is a registration bug, not a runtime surprise.
    description: str
    trusts_input: bool
    handler: Handler
    # F46/T2.0: optional. Given `(ctx, params)`, returns a dict of
    # human-meaningful values to overlay onto `params` for CONFIRM-prompt
    # display only — e.g. open_app's real `{"app": "notepad"}` becomes
    # `{"app": "C:\\...\\notepad.exe"}` in what a human is asked to approve,
    # without changing what's audited or handed to the handler. See
    # SkillSpec.invoke() for the two guarantees this buys: what's shown is
    # resolved *before* the ladder decides (not the raw free-text name a
    # human never actually consented to), and invoke() calls it again right
    # after the ladder decides, refusing rather than proceeding if the two
    # calls disagree (docs/THREAT_MODEL.md's realpath-at-write-time rule's
    # sibling — a resolution made during a still-open confirm dialog is not
    # trusted to still mean the same thing once a human has answered it).
    # Never a substitute for the handler's OWN call-time revalidation —
    # open_app.py's handler still resolves and verifies independently,
    # every call, exactly as before this existed.
    resolve_for_confirm: ResolveForConfirm | None = None
    # Exact command shapes (2026-10-15): regexes matched against the whole
    # normalized sentence before fuzzy example matching. Named groups become
    # handler params, so a skill can take several ("set volume to 40").
    patterns: tuple["re.Pattern[str]", ...] = ()
    # Broad patterns ("open X") that only count when no other skill's
    # `patterns` matched -- so "open youtube" goes to open_website and
    # "open wifi settings" to open_settings, and only the rest is an app.
    catch_all_patterns: tuple["re.Pattern[str]", ...] = ()
    # The reply text can carry outside-world text (a geocoded place name, a
    # window title). The agent brain treats it like web content: later tool
    # calls in that conversation need approval.
    untrusted_output: bool = False
    # How NOVA asks out loud for approval, e.g. "close {app}" -> "Should I close spotify?"
    confirm_phrase: str | None = None

    async def invoke(self, ctx: SkillCtx, **params: Any) -> Reply:
        # Layer 2 of docs/SECURITY_SPEC.md §1.3: the skill-parameter binder's
        # own runtime guard, independent of whatever guard the caller (today,
        # only the router) may or may not have already run. This must be the
        # very first thing that happens — before ladder authorization and
        # long before the handler — because both of those are exactly where
        # an Untrusted value would become a tool-call parameter, which is the
        # one thing the constitution forbids absolutely. Do not move this
        # below `ctx.ladder.authorize()`: authorize() itself records `params`
        # into the audit log (see Ladder._record), so even reaching the
        # ladder with an Untrusted value still in `params` would be a leak.
        for name, value in params.items():
            hit = _find_untrusted(value, name)
            if hit is not None:
                raise TypeError(
                    f"skill '{self.name}': parameter {hit!r} is Untrusted content — "
                    "reveal() it explicitly inside a trusts_input=True handler, or do "
                    "not bind it as a tool-call parameter at all "
                    "(docs/SECURITY_SPEC.md section 1.3, layer 2)"
                )

        # F50 (nova-security): bind params against the handler's REAL
        # signature before the ladder ever sees them — not just before the
        # handler runs. Without this, a malformed proposal (extra/missing/
        # misnamed params — the shape a Claude-proposed action per T2.4
        # could realistically get wrong, since nothing there is
        # router-derived) would sail through authorize(), possibly pop a
        # real CONFIRM dialog and audit a real "allow", and only THEN fail
        # with a TypeError once self.handler(ctx, **params) actually ran —
        # asking a human to approve a call that was never going to execute.
        # `Signature.bind()` raises the identical TypeError `handler(ctx,
        # **params)` would have (same stdlib machinery), just relocated
        # earlier — well-formed calls are unaffected.
        try:
            inspect.signature(self.handler).bind(ctx, **params)
        except TypeError as exc:
            raise TypeError(f"skill '{self.name}': {exc}") from exc

        # F46/T2.0: resolve once now, before the ladder decides anything —
        # this is the value (if any) Ladder.authorize() shows a human in a
        # CONFIRM prompt, in place of raw free-text params. Computed
        # unconditionally whenever a skill declares resolve_for_confirm
        # (not just when this particular call is actually CONFIRM-tier):
        # SkillSpec.invoke() has no cheap way to know the ladder's eventual
        # effective tier ahead of calling it (that composition — declared
        # tier vs. an F45 floor and now F28's ceiling — is
        # Ladder.authorize()'s job, not this module's to duplicate), and a
        # resolve hook is defined to be a cheap, side-effect-free lookup
        # (open_app.py's is a path resolve+containment check), so the
        # wasted call on an ACT/READ path costs nothing worth avoiding at
        # this complexity.
        pre_resolution: dict[str, Any] | None = None
        if self.resolve_for_confirm is not None:
            try:
                pre_resolution = await self.resolve_for_confirm(ctx, params)
            except Exception as exc:  # noqa: BLE001 — a broken resolve hook must not crash the turn or block a legitimate call
                logger.warning(
                    "skill '%s': resolve_for_confirm raised before authorize() (%s) — "
                    "the CONFIRM prompt, if any, will show raw params instead",
                    self.name, exc,
                )
                pre_resolution = None

        # F28: the ceiling this call is subject to, derived honestly from
        # how the turn was armed (ctx.armed_by, threaded from nova/app.py's
        # bus handlers — see SkillCtx's own docstring) rather than guessed
        # here. ArmedBy.PTT (a human pressed the hotkey) means no ceiling
        # at all; ArmedBy.WAKE means Tier.READ, Manav's ruling. This module
        # only translates the arming source into a ceiling value —
        # Ladder.authorize() is the one place that actually enforces it,
        # same separation of concerns as the floor/confirm_display above.
        # Explicit allow-list (security review 2026-10-14): a future arming
        # source must be given its ruling here, or it gets READ -- fail closed.
        ceiling = None if ctx.armed_by in (ArmedBy.PTT, ArmedBy.VOICE, ArmedBy.TYPED) else Tier.READ

        # T2.4: the floor this call is subject to, derived the identical
        # way from ctx.origin — Origin.ROUTER (a local match) means no
        # floor; Origin.CLAUDE means Tier.CONFIRM, the provenance rule
        # (docs/THREAT_MODEL_CLAUDE_FALLTHROUGH.md finding 2/4: the same
        # action Manav asked for by voice and one a model inferred are
        # different events, and READ is exempt regardless — Ladder.
        # authorize() already carries that exemption, nothing to duplicate
        # here). Computed right alongside `ceiling` on purpose: a Claude-
        # proposed action in a wake-armed turn sets BOTH on the same call,
        # which is exactly the composition authorize() resolves (ceiling
        # wins) — this is where that becomes reachable from a real
        # proposal, not just a direct authorize() call.
        floor = Tier.CONFIRM if ctx.origin is Origin.CLAUDE else None
        origin = ctx.origin.value if ctx.origin is Origin.CLAUDE else None

        # Every caller of a skill goes through here, so this is the one place
        # that enforces the permission ladder and produces an audit record
        # for every invocation, including READ ("free" means no confirmation
        # friction, not unaudited — docs/THREAT_MODEL.md §4).
        decision = await ctx.ladder.authorize(
            self, params, floor=floor, ceiling=ceiling, origin=origin, confirm_display=pre_resolution,
        )
        if not decision.allowed:
            spoken = _spoken_denial_text(decision.reason)
            return Reply(text=spoken, success=False)

        # F46/T2.0: the TOCTOU close. A CONFIRM dialog can sit open for as
        # long as the channel's own timeout allows (up to 60s today,
        # nova/security/confirm.py) — plenty of time for whatever
        # resolve_for_confirm looked up (an allowlist entry, a resolved
        # path) to change underneath the human's answer. Re-resolve now,
        # right before the handler would run, and refuse rather than
        # silently proceeding on a DIFFERENT reality than the one that was
        # actually shown and approved. A resolve hook that raises here is
        # treated the same as a disagreement — proceeding on an answer we
        # can no longer verify is exactly the "unresolved by the ladder"
        # gap this exists to close, not a reason to be lenient.
        if self.resolve_for_confirm is not None:
            try:
                post_resolution = await self.resolve_for_confirm(ctx, params)
            except Exception as exc:  # noqa: BLE001 — see above: unverifiable is treated as changed, not ignored
                logger.error(
                    "skill '%s': resolve_for_confirm raised after authorize() (%s) — "
                    "refusing rather than proceeding unverified",
                    self.name, exc,
                )
                ctx.ladder.record_denial(self, params, "confirm_resolution_unverifiable")
                return Reply(
                    text=f"I can't confirm '{self.name}' still means what I asked about — please try again.",
                    success=False,
                )
            if post_resolution != pre_resolution:
                logger.warning(
                    "skill '%s': resolve_for_confirm changed between the confirm prompt and the decision "
                    "(%r -> %r) — refusing rather than proceeding on a different reality than what was shown",
                    self.name, pre_resolution, post_resolution,
                )
                ctx.ladder.record_denial(self, params, "confirm_resolution_changed")
                return Reply(
                    text=f"What '{self.name}' would actually do just changed — please ask again to be safe.",
                    success=False,
                )

        return await self.handler(ctx, **params)


_REGISTRY: dict[str, SkillSpec] = {}


def skill(
    *,
    name: str,
    tier: Tier,
    examples: list[str],
    description: str,
    trusts_input: bool,
    resolve_for_confirm: ResolveForConfirm | None = None,
    patterns: list[str] | None = None,
    confirm_phrase: str | None = None,
    catch_all_patterns: list[str] | None = None,
    untrusted_output: bool = False,
) -> Callable[[Handler], Handler]:
    """Decorator: registers `name` in the module-level registry at import
    time. Raises SkillError on a duplicate name, an empty examples list, an
    empty description, or a `tier` that isn't a Tier member — all
    import-time, all loud.

    `description` (T2.4) is a short, one-line summary written for an
    external model (`nova.brain.claude_arm`) reading it cold — see
    `SkillSpec.description`'s own comment for why this is deliberately not
    `handler.__doc__`.

    `resolve_for_confirm` (F46/T2.0) is optional and almost always omitted —
    only worth declaring for a skill whose params are a free-text name that
    resolves to something a human should see resolved before approving it
    (open_app.py's is the first; see its docstring for the contract)."""

    def decorator(handler: Handler) -> Handler:
        if not isinstance(tier, Tier):
            raise SkillError(
                f"skill '{name}': tier must be a nova.security.tiers.Tier member, "
                f"got {tier!r}"
            )
        if not examples:
            raise SkillError(f"skill '{name}': must declare at least one example")
        if not description or not description.strip():
            raise SkillError(f"skill '{name}': must declare a non-empty description")
        param_names = [p for p in inspect.signature(handler).parameters if p != "ctx"]

        def _compile(pats):
            out: list[re.Pattern[str]] = []
            for pat in pats or ():
                try:
                    rx = re.compile(pat, re.IGNORECASE)
                except re.error as exc:
                    raise SkillError(f"skill '{name}': bad pattern {pat!r}: {exc}") from exc
                unknown = set(rx.groupindex) - set(param_names)
                if unknown:
                    raise SkillError(f"skill '{name}': pattern {pat!r} names groups {sorted(unknown)} the handler doesn't accept")
                out.append(rx)
            return out

        catch_all = _compile(catch_all_patterns)
        compiled = _compile(patterns)
        if name in _REGISTRY:
            existing = _REGISTRY[name]
            raise SkillError(
                f"duplicate skill name '{name}': already registered by "
                f"{existing.handler.__module__}.{existing.handler.__qualname__}, "
                f"cannot re-register from {handler.__module__}.{handler.__qualname__}"
            )
        _REGISTRY[name] = SkillSpec(
            name=name,
            tier=tier,
            examples=tuple(examples),
            description=description,
            trusts_input=trusts_input,
            handler=handler,
            resolve_for_confirm=resolve_for_confirm,
            patterns=tuple(compiled),
            confirm_phrase=confirm_phrase,
            catch_all_patterns=tuple(catch_all),
            untrusted_output=untrusted_output,
        )
        return handler

    return decorator


def discover_skills(package: str = "nova.skills") -> dict[str, SkillSpec]:
    """Import every module under nova/skills/ (except this one) so their
    @skill decorators run, then return the registry.

    Safe to call more than once: Python caches imported modules, so a
    second call re-imports nothing and re-registers nothing — it just
    returns the current registry state.
    """
    pkg = importlib.import_module(package)
    this_module = f"{package}.registry"
    for _finder, module_name, _is_pkg in pkgutil.iter_modules(pkg.__path__, prefix=f"{package}."):
        if module_name == this_module:
            continue
        importlib.import_module(module_name)
    return dict(_REGISTRY)


def all_skills() -> dict[str, SkillSpec]:
    """The current registry, as populated by discover_skills(). The router
    reads `.examples`, the ladder reads `.tier`, the injection suite
    enumerates entries with `.trusts_input == True`."""
    return dict(_REGISTRY)


def get_skill(name: str) -> SkillSpec | None:
    return _REGISTRY.get(name)


def _reset_registry_for_tests() -> None:
    """Not part of the public contract. nova-tester needs a way to clear the
    module-level registry between test cases that exercise duplicate-name
    detection; exposed explicitly rather than reaching into `_REGISTRY`."""
    _REGISTRY.clear()
