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
import pkgutil
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from nova.config import Config
from nova.log import get_logger
from nova.security.tiers import Tier
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
    """

    config: Config
    ladder: "Ladder"


@dataclass(frozen=True)
class SkillSpec:
    name: str
    tier: Tier
    examples: tuple[str, ...]
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
        # tier vs. an F28/T2.4 floor — is Ladder.authorize()'s job, not
        # this module's to duplicate), and a resolve hook is defined to be
        # a cheap, side-effect-free lookup (open_app.py's is a path
        # resolve+containment check), so the wasted call on an ACT/READ
        # path costs nothing worth avoiding at this complexity.
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

        # Every caller of a skill goes through here, so this is the one place
        # that enforces the permission ladder and produces an audit record
        # for every invocation, including READ ("free" means no confirmation
        # friction, not unaudited — docs/THREAT_MODEL.md §4).
        decision = await ctx.ladder.authorize(self, params, confirm_display=pre_resolution)
        if not decision.allowed:
            return Reply(text=decision.reason, success=False)

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
    trusts_input: bool,
    resolve_for_confirm: ResolveForConfirm | None = None,
) -> Callable[[Handler], Handler]:
    """Decorator: registers `name` in the module-level registry at import
    time. Raises SkillError on a duplicate name, an empty examples list, or
    a `tier` that isn't a Tier member — all import-time, all loud.

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
            trusts_input=trusts_input,
            handler=handler,
            resolve_for_confirm=resolve_for_confirm,
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
