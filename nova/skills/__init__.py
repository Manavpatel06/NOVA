"""Skill package. Importing this package is what triggers discovery — no
other module needs to remember to call discover_skills() (docs/CONTRACTS.md
T0.5: "Registry is import-time discovery of nova/skills/*.py")."""

from __future__ import annotations

from nova.skills.registry import Reply, SkillCtx, SkillError, SkillSpec, all_skills, discover_skills, get_skill

discover_skills()

__all__ = [
    "Reply",
    "SkillCtx",
    "SkillError",
    "SkillSpec",
    "all_skills",
    "discover_skills",
    "get_skill",
]
