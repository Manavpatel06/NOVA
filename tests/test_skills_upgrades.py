"""The voice side of NOVA upgrading itself (ADR 0021)."""
from __future__ import annotations

import dataclasses

import pytest

import nova.skills  # noqa: F401
from nova.brain.hub_tier import catalogue, check
from nova.brain.router import Router
from nova.config import Config, HubConfig
from nova.security.egress import EgressError
from nova.security.tiers import ArmedBy, Tier
from nova.skills import upgrades
from nova.skills.registry import SkillCtx, all_skills, get_skill


class Hub:
    def __init__(self, replies=None, exc=None):
        self.replies, self.exc, self.calls = replies or {}, exc, []

    def post_json(self, url, body, *, headers, timeout=20.0, method="POST"):
        path = url.split("ts.net", 1)[1]
        self.calls.append((path, body))
        if self.exc:
            raise self.exc
        return self.replies.get(path, {"ok": True})


def _ctx(egress, host="nova-hub.tail1.ts.net"):
    cfg = dataclasses.replace(Config(), hub=dataclasses.replace(HubConfig(), host=host))
    return SkillCtx(config=cfg, ladder=None, egress=egress, armed_by=ArmedBy.VOICE)


async def test_asking_for_an_upgrade_starts_a_build_on_the_hub():
    hub = Hub({"/v1/forge/request": {"ok": True, "id": "abcd1234"}})
    reply = await upgrades.upgrade_nova(_ctx(hub), request="flip a coin")
    assert hub.calls == [("/v1/forge/request", {"request": "flip a coin"})]
    assert reply.success and "ping your phone" in reply.text


async def test_status_reads_the_latest_job():
    hub = Hub({"/v1/forge/list": {"jobs": [{"request": "flip a coin", "status": "ready",
                                            "summary": "Say 'Nova, flip a coin'.", "detail": ""}]}})
    reply = await upgrades.upgrade_status(_ctx(hub))
    assert "flip a coin is ready" in reply.text and "apply the upgrade" in reply.text


READY = {"id": "abcd1234", "commit": "c" * 40, "request": "flip a coin", "files": ["nova/skills/coin.py",
                                                                              "tests/test_coin.py"]}


async def test_apply_installs_exactly_the_ready_commit_here(monkeypatch):
    from nova.system import winops

    installed = []
    monkeypatch.setattr(winops, "git_update", lambda repo, commit: installed.append(commit))
    hub = Hub({"/v1/forge/ready": {"ready": READY}, "/v1/forge/apply": {"ok": True, "pushed": True}})
    reply = await upgrades.apply_upgrade(_ctx(hub))
    assert ("/v1/forge/apply", {"id": "abcd1234", "commit": "c" * 40}) in hub.calls
    assert installed == ["c" * 40] and "reopen" in reply.text


async def test_the_confirm_prompt_names_what_will_be_installed():
    hub = Hub({"/v1/forge/ready": {"ready": READY}})
    shown = await upgrades._what_will_be_installed(_ctx(hub), {})
    assert shown == {"upgrade": "flip a coin (new files: coin.py, test_coin.py; version ccccccc)"}
    assert (await upgrades._what_will_be_installed(_ctx(Hub({"/v1/forge/ready": {"ready": None}})), {})) == {
        "upgrade": "nothing -- no upgrade is ready"}


async def test_apply_without_github_says_what_to_do(monkeypatch):
    from nova.system import winops

    monkeypatch.setattr(winops, "git_update", lambda repo, commit: pytest.fail("must not install"))
    hub = Hub({"/v1/forge/ready": {"ready": READY}, "/v1/forge/apply": {"ok": True, "pushed": False}})
    reply = await upgrades.apply_upgrade(_ctx(hub))
    assert not reply.success and "nova-hub github" in reply.text


async def test_nothing_ready_installs_nothing():
    hub = Hub({"/v1/forge/ready": {"ready": None}})
    reply = await upgrades.apply_upgrade(_ctx(hub))
    assert not reply.success and all(c[0] != "/v1/forge/apply" for c in hub.calls)


async def test_no_hub():
    assert (await upgrades.upgrade_nova(_ctx(Hub(), host=""), request="flip a coin")).text == upgrades.NO_HUB
    assert (await upgrades.apply_upgrade(_ctx(Hub(exc=EgressError("down"))))).text == upgrades.NO_HUB


def test_building_and_installing_both_need_manavs_confirmation_and_the_hub_lane_never_does_them():
    assert get_skill("apply_upgrade").tier is Tier.CONFIRM and get_skill("upgrade_nova").tier is Tier.CONFIRM
    assert get_skill("apply_upgrade").resolve_for_confirm is not None
    assert "upgrade_nova" not in {t["name"] for t in catalogue(all_skills())}
    assert "apply_upgrade" not in {t["name"] for t in catalogue(all_skills())}
    d = {"route": "tool", "tool": "apply_upgrade", "params": {}, "say": ""}
    assert check(d, "apply the upgrade", all_skills()) is None


@pytest.mark.parametrize("text,skill", [
    ("add a skill that tells me a joke", "upgrade_nova"),
    ("upgrade yourself so you can flip a coin", "upgrade_nova"),
    ("teach yourself to convert units", "upgrade_nova"),
    ("is the upgrade ready", "upgrade_status"),
    ("apply the upgrade", "apply_upgrade"),
    ("throw away the upgrade", "discard_upgrade"),
])
def test_routing(text, skill):
    m = Router(confidence_floor=0.42, ambiguity_margin=0.08).route(text)
    assert m.spec is not None and m.spec.name == skill


def test_git_update_is_windows_only_pinned_to_a_commit_and_never_a_shell(monkeypatch):
    import inspect

    from nova.system import winops

    with pytest.raises(ValueError):
        winops.git_update("D:\\NOVA", "main")
    monkeypatch.setattr(winops.sys, "platform", "linux")
    with pytest.raises(winops.WinOpsUnavailable):
        winops.git_update("D:\\NOVA", "c" * 40)
    src = inspect.getsource(winops.git_update)
    assert "shell=False" in src and '"--ff-only"' in src and "FETCH_HEAD" in src and "core.hooksPath" in src
