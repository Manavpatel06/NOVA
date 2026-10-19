"""System volume skills (nova/skills/volume.py): routing, params, replies,
error paths. winops.powershell is always monkeypatched -- nothing touches
the real audio device."""
from __future__ import annotations

import re

import pytest

import nova.skills  # noqa: F401  (populates the registry)
from nova.brain.router import Router
from nova.config import Config
from nova.security.audit import AuditLog
from nova.security.ladder import Ladder
from nova.security.tiers import Tier
from nova.skills import volume as vol
from nova.skills.registry import SkillCtx, get_skill
from nova.system import winops


@pytest.fixture
def ctx(tmp_path):
    return SkillCtx(config=Config(), ladder=Ladder(AuditLog(path=tmp_path / "audit.log")))


@pytest.fixture
def fake_ps(monkeypatch):
    """Simulated endpoint: records calls, applies ops to a level/mute state."""
    state = {"level": 30, "muted": False, "calls": []}

    def fake(script, *, timeout=15.0, **args):
        assert script is vol._VOLUME_PS
        state["calls"].append(dict(args))
        op, value = args["op"], int(args.get("value", 0))
        if op == "set":
            state["level"] = max(0, min(100, value))
            if value > 0:
                state["muted"] = False
        elif op == "step":
            state["level"] = max(0, min(100, state["level"] + value))
            if value > 0:
                state["muted"] = False
        elif op == "mute":
            state["muted"] = value != 0
        return f"{state['level']} {1 if state['muted'] else 0}"

    monkeypatch.setattr(winops, "powershell", fake)
    return state


def _route(text):
    return Router().route(text)


@pytest.mark.parametrize("text,skill,params", [
    ("set volume to 40", "set_volume", {"level": "40"}),
    ("volume 70 percent", "set_volume", {"level": "70"}),
    ("Set the volume to 55%.", "set_volume", {"level": "55"}),
    ("set the volume to forty five", "set_volume", {"level": "forty five"}),
    ("turn the volume up to 80", "set_volume", {"level": "80"}),
    ("max volume", "set_volume", {"level": "max"}),
    ("turn it up", "volume_up", {}),
    ("louder", "volume_up", {}),
    ("volume up", "volume_up", {}),
    ("turn up the volume", "volume_up", {}),
    ("volume up by 20", "volume_up", {"amount": "20"}),
    ("can you make it louder please", "volume_up", {}),
    ("turn the volume down a bit", "volume_down", {}),
    ("turn it down", "volume_down", {}),
    ("quieter", "volume_down", {}),
    ("lower the volume by 15 percent", "volume_down", {"amount": "15"}),
    ("mute", "mute", {}),
    ("mute the sound", "mute", {}),
    ("can you mute", "mute", {}),
    ("unmute", "unmute", {}),
    ("unmute the sound", "unmute", {}),
    ("what's the volume", "get_volume", {}),
    ("what is the volume at", "get_volume", {}),
    ("is it muted", "get_volume", {}),
])
def test_volume_sentences_route_with_params(text, skill, params):
    m = _route(text)
    assert m.clarification is None, m.clarification
    assert m.spec is not None and m.spec.name == skill
    assert m.params == params


@pytest.mark.parametrize("text,owner", [
    ("next song", "media_next"),
    ("open notepad", "open_app"),
    ("open sound settings", "open_settings"),
    ("what time is it", "time"),
])
def test_volume_patterns_do_not_steal_other_commands(text, owner):
    m = _route(text)
    assert m.spec is not None and m.spec.name == owner


@pytest.mark.parametrize("text", ["pause", "play", "skip", "turn on bluetooth", "brightness up"])
def test_media_and_other_commands_never_land_on_a_volume_skill(text):
    m = _route(text)
    assert m.spec is None or m.spec.name not in {
        "set_volume", "volume_up", "volume_down", "mute", "unmute", "get_volume"}


def test_tiers():
    for name in ("set_volume", "volume_up", "volume_down", "mute", "unmute"):
        assert get_skill(name).tier is Tier.ACT
    assert get_skill("get_volume").tier is Tier.READ


@pytest.mark.parametrize("text,expected", [
    ("40", 40), ("40%", 40), ("40 percent", 40), ("forty", 40), ("forty-five", 45),
    ("forty five", 45), ("seven", 7), ("max", 100), ("full", 100), ("a hundred", 100),
    ("minimum", 0), ("loud", None), ("", None), (None, None),
])
def test_parse_level(text, expected):
    assert vol.parse_level(text) == expected


@pytest.mark.parametrize("amount,expected", [
    ("", 10), (None, 10), ("20", 20), ("a bit", 10), ("little", 10), ("0", None), ("500", None), ("loads", None),
])
def test_parse_step(amount, expected):
    assert vol.parse_step(amount) == expected


async def test_set_volume_reply_and_args(ctx, fake_ps):
    reply = await get_skill("set_volume").invoke(ctx, level="40")
    assert reply.success and reply.text == "Volume's at 40."
    assert fake_ps["calls"] == [{"op": "set", "value": 40}]


async def test_set_volume_word_number(ctx, fake_ps):
    reply = await get_skill("set_volume").invoke(ctx, level="seventy")
    assert reply.text == "Volume's at 70."


@pytest.mark.parametrize("bad", ["150", "loud", "999"])
async def test_set_volume_rejects_out_of_range_without_calling_windows(ctx, fake_ps, bad):
    reply = await get_skill("set_volume").invoke(ctx, level=bad)
    assert reply.success is False and "0 to 100" in reply.text
    assert fake_ps["calls"] == []


async def test_volume_up_default_step_and_by_amount(ctx, fake_ps):
    reply = await get_skill("volume_up").invoke(ctx)
    assert reply.text == "Volume's at 40."
    assert fake_ps["calls"][-1] == {"op": "step", "value": 10}
    reply = await get_skill("volume_up").invoke(ctx, amount="20")
    assert reply.text == "Volume's at 60."


async def test_volume_down_is_a_negative_step_and_clamps(ctx, fake_ps):
    reply = await get_skill("volume_down").invoke(ctx, amount="50")
    assert fake_ps["calls"][-1] == {"op": "step", "value": -50}
    assert reply.text == "Volume's at 0."


async def test_mute_unmute_and_get(ctx, fake_ps):
    assert (await get_skill("mute").invoke(ctx)).text == "Muted."
    assert fake_ps["calls"][-1] == {"op": "mute", "value": 1}
    assert (await get_skill("get_volume").invoke(ctx)).text == "Volume's at 30, but it's muted."
    assert (await get_skill("unmute").invoke(ctx)).text == "Unmuted. Volume's at 30."
    assert fake_ps["calls"][-1] == {"op": "mute", "value": 0}
    assert (await get_skill("get_volume").invoke(ctx)).text == "Volume's at 30."


async def test_end_to_end_from_speech(ctx, fake_ps):
    m = _route("Could you turn the volume up by 20, please?")
    reply = await m.spec.invoke(ctx, **m.params)
    assert reply.text == "Volume's at 50."


@pytest.mark.parametrize("exc", [winops.WinOpsUnavailable("no"), winops.WinOpsError("boom")])
async def test_windows_failures_become_honest_spoken_refusals(ctx, monkeypatch, exc):
    def boom(*a, **k):
        raise exc
    monkeypatch.setattr(winops, "powershell", boom)
    reply = await get_skill("set_volume").invoke(ctx, level="40")
    assert reply.success is False and reply.text == "I couldn't change the volume."
    reply = await get_skill("get_volume").invoke(ctx)
    assert reply.success is False and reply.text == "I couldn't read the volume."


async def test_garbled_powershell_output_is_a_failure_not_a_crash(ctx, monkeypatch):
    monkeypatch.setattr(winops, "powershell", lambda *a, **k: "Exception: something")
    reply = await get_skill("volume_up").invoke(ctx)
    assert reply.success is False


async def test_on_linux_the_real_winops_refuses_honestly(ctx):
    """No monkeypatch: the real winops raises WinOpsUnavailable off Windows."""
    import sys
    if sys.platform == "win32":
        pytest.skip("Windows would really change the volume")
    reply = await get_skill("mute").invoke(ctx)
    assert reply.success is False


# --- the inline C#: one constant, correct COM identities and vtable order ---

def test_core_audio_csharp_is_one_constant_shared_by_every_action():
    assert vol._AUDIO_CS in vol._VOLUME_PS
    assert vol._VOLUME_PS.count("Add-Type") == 1


def test_core_audio_guids():
    cs = vol._AUDIO_CS
    assert 'Guid("BCDE0395-E52F-467C-8E3D-C4579291692E")' in cs  # CLSID_MMDeviceEnumerator
    assert 'Guid("A95664D2-9614-4F35-A746-DE8DB63617E6")' in cs  # IID_IMMDeviceEnumerator
    assert 'Guid("D666063F-1587-4E43-81F1-B948E807363F")' in cs  # IID_IMMDevice
    assert 'Guid("5CDF2C82-841E-4546-9722-0CF74078229A")' in cs  # IID_IAudioEndpointVolume


def _methods(cs, iface):
    body = re.search(r"interface " + iface + r" \{(.*?)\n    \}", cs, re.S).group(1)
    return re.findall(r"(\w+)\(", re.sub(r"\[[^\]]*\]", "", body))


def test_iaudioendpointvolume_vtable_order_matches_endpointvolume_h():
    assert _methods(vol._AUDIO_CS, "IAudioEndpointVolume") == [
        "RegisterControlChangeNotify", "UnregisterControlChangeNotify", "GetChannelCount",
        "SetMasterVolumeLevel", "SetMasterVolumeLevelScalar", "GetMasterVolumeLevel",
        "GetMasterVolumeLevelScalar", "SetChannelVolumeLevel", "SetChannelVolumeLevelScalar",
        "GetChannelVolumeLevel", "GetChannelVolumeLevelScalar", "SetMute", "GetMute",
    ]


def test_mmdevice_vtable_order_matches_mmdeviceapi_h():
    assert _methods(vol._AUDIO_CS, "IMMDeviceEnumerator") == ["EnumAudioEndpoints", "GetDefaultAudioEndpoint"]
    assert _methods(vol._AUDIO_CS, "IMMDevice") == ["Activate"]


def test_bools_are_marshalled_as_win32_bool_not_variant_bool():
    assert "[MarshalAs(UnmanagedType.Bool)] bool bMute" in vol._AUDIO_CS
    assert "[return: MarshalAs(UnmanagedType.Bool)] bool GetMute()" in vol._AUDIO_CS


def test_script_has_no_blank_line_inside_a_multiline_construct():
    """winops feeds `-Command -` on stdin, which reads like an interactive
    console: a blank line inside a here-string/brace block could end it early."""
    depth = 0
    in_here = False
    for line in vol._VOLUME_PS.split("\n"):
        if not line.strip():
            assert depth == 0 and not in_here, "blank line inside a multi-line statement"
            continue
        if line.rstrip().endswith("@'"):
            in_here = True
            continue
        if in_here:
            if line.startswith("'@"):
                in_here = False
            continue
        depth += line.count("{") - line.count("}")
