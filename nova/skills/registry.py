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

if TYPE_CHECKING:
    # Deferred to break the registry.py <-> ladder.py import cycle: SkillCtx
    # holds a Ladder, Ladder.authorize() takes a SkillSpec -- this module
    # only ever uses Ladder in a type annotation (never isinstance-checked),
    # and `from __future__ import annotations` above makes every annotation
    # a string at runtime, so a real import here is never needed. Mirrors
    # the same move on ladder.py's side (nova-dev-sec, c1ea631).
    from nova.security.ladder import Ladder

Handler = Callable[..., Awaitable["Reply"]]


class SkillError(ValueError):
    """Raised for a skill registration conflict (duplicate name, missing
    examples, wrong tier type) — always at import time, never at call time."""


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
