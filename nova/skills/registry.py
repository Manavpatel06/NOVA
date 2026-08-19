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
import pkgutil
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from nova.config import Config
from nova.security.tiers import Tier
from nova.security.untrusted import Untrusted

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

        # Every caller of a skill goes through here, so this is the one place
        # that enforces the permission ladder and produces an audit record
        # for every invocation, including READ ("free" means no confirmation
        # friction, not unaudited — docs/THREAT_MODEL.md §4).
        decision = await ctx.ladder.authorize(self, params)
        if not decision.allowed:
            return Reply(text=decision.reason, success=False)
        return await self.handler(ctx, **params)


_REGISTRY: dict[str, SkillSpec] = {}


def skill(
    *,
    name: str,
    tier: Tier,
    examples: list[str],
    trusts_input: bool,
) -> Callable[[Handler], Handler]:
    """Decorator: registers `name` in the module-level registry at import
    time. Raises SkillError on a duplicate name, an empty examples list, or
    a `tier` that isn't a Tier member — all import-time, all loud."""

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
