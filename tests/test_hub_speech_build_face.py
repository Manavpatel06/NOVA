"""Speaking in pieces, building with Claude Code (ADR 0027), and NOVA's face
on the hub's screen (ADR 0028)."""
from __future__ import annotations

import base64
import json
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from nova.hub.builder import Builder, last_paragraph, slugify
from nova.hub.face import Face
from nova.hub.service import HubError, HubService
from nova.hub.talk import PIECE_MAX, Talk, split_speech
from nova.hub.voice import to_wav


# --- speaking in pieces ----------------------------------------------------------------

def test_short_replies_stay_one_piece():
    assert split_speech("It's 7 PM.") == ["It's 7 PM."]
    assert split_speech("") == [""]


def test_long_replies_start_with_a_short_piece_and_lose_nothing():
    text = ("Here's a quick one, Manav. The laptop hums, the coffee cools, and the morning stays patient: a small "
            "poem for a Wednesday in Tempe. Want me to make it longer? Or change the mood entirely? I can do both.")
    pieces = split_speech(text)
    assert len(pieces) >= 2 and len(pieces[0]) <= 60
    assert " ".join(pieces) == " ".join(text.split())
    assert all(len(p) <= PIECE_MAX for p in pieces)


def test_one_huge_sentence_is_still_cut():
    pieces = split_speech("word " * 200)
    assert all(len(p) <= PIECE_MAX + 1 for p in pieces) and len(pieces) > 3


class _Voice:
    def __init__(self, delay=0.0):
        self.spoken = []
        self.delay = delay

    def ready(self):
        return True

    def speak(self, text):
        time.sleep(self.delay)
        self.spoken.append(text)
        return to_wav(text.encode()[:2].ljust(2, b"\0") * 10, 16000)

    def transcribe(self, pcm):
        return "hello"


class _Says:
    def __init__(self, reply):
        self.reply = reply

    async def turn(self, text, channel):
        return self.reply

    async def warm_up(self):
        pass


LONG = "First, a short one. " + "Then a much longer sentence that goes on for a while about many things. " * 4


def test_the_first_piece_comes_back_and_the_rest_follow_in_order():
    voice = _Voice()
    talk = Talk(_Says(LONG), voice)
    out = talk.handle({"session": "abcdefgh", "text": "go"})
    assert out["say"] == LONG and out["audio"] and out["more"] and out["parts"] >= 1
    rest = [talk.more({"id": out["more"], "part": i}) for i in range(out["parts"])]
    assert all(r["audio"] for r in rest) and rest[-1]["last"] is True
    assert all(r["last"] is False for r in rest[:-1])
    assert voice.spoken == split_speech(LONG)          # every piece, once, in order


def test_more_waits_for_a_piece_still_being_made():
    talk = Talk(_Says(LONG), _Voice(delay=0.2))
    out = talk.handle({"session": "abcdefgh", "text": "go"})
    t = time.monotonic()
    assert talk.more({"id": out["more"], "part": out["parts"] - 1})["audio"]
    assert time.monotonic() - t >= 0.15


@pytest.mark.parametrize("body", [None, {}, {"id": "nope", "part": 0}, {"id": "x", "part": True},
                                  {"id": "x", "part": "0"}])
def test_bad_more_requests(body):
    with pytest.raises(HubError):
        Talk(_Says("hi"), _Voice()).more(body)


def test_a_part_out_of_range_is_refused():
    talk = Talk(_Says(LONG), _Voice())
    out = talk.handle({"session": "abcdefgh", "text": "go"})
    with pytest.raises(HubError):
        talk.more({"id": out["more"], "part": out["parts"]})


def test_the_laptop_plays_every_piece_in_order():
    from nova.ears import Ears

    talk = Talk(_Says(LONG), _Voice())
    played = []
    ears = Ears(lambda pcm: talk.handle({"session": "abcdefgh", "text": "go"}), play=played.append,
                more=lambda sid, part: talk.more({"id": sid, "part": part}))
    out = ears.handle(b"\x00\x00")
    assert len(played) == 1 + out["parts"]


# --- building with Claude Code -----------------------------------------------------------

def test_project_names():
    assert slugify("build me a website for my portfolio") == "website-portfolio"
    assert slugify("") == "project" and slugify("!!!") == "project"
    assert len(slugify("a " + "very long words " * 20)) <= 40
    assert slugify("../../etc/passwd") == "etc-passwd"


def test_last_paragraph_is_what_gets_said():
    assert last_paragraph("Did lots.\n\nBuilt **hello.py**. Run `python hello.py`.") == "Built hello.py. Run python hello.py."


def _fake_runner(result="All done.\n\nI built the site. Open index.html.", code=0, seen=None, gate=None):
    def run(argv, cwd, timeout, env):
        if seen is not None:
            seen.append((argv, cwd))
        if gate is not None:
            gate.wait(5)
        (Path(cwd) / "index.html").write_text("<h1>hi</h1>")
        return code, json.dumps({"type": "result", "result": result})
    return run


def _wait(b, until=("done", "failed")):
    for _ in range(100):
        if b.list() and b.list()[0]["status"] in until:
            return b.list()[0]
        time.sleep(0.05)
    raise AssertionError("build didn't finish")


def test_a_build_runs_in_its_project_folder_and_reports_back(tmp_path):
    said, seen = [], []
    b = Builder(projects=tmp_path / "Projects", state=tmp_path / "b.json", notify=said.append,
                runner=_fake_runner(seen=seen), sandbox=False)
    out = b.start("a portfolio website with three pages", "portfolio")
    done = _wait(b)
    assert out["project"] == "portfolio" and done["status"] == "done"
    assert done["summary"] == "I built the site. Open index.html."
    assert seen[0][1] == tmp_path / "Projects" / "portfolio" and (tmp_path / "Projects/portfolio/index.html").exists()
    argv = seen[0][0]
    assert "--permission-mode" in argv and "bypassPermissions" in argv and "--safe-mode" not in argv  # his skills on
    assert said and "portfolio" in said[0]


def test_the_sandboxed_command_hides_the_hub_and_protects_claudes_config(tmp_path, monkeypatch):
    import nova.hub.builder as builder

    seen = []
    monkeypatch.setattr(builder.shutil, "which", lambda n: f"/usr/bin/{n}")
    b = Builder(projects=tmp_path / "P", state=tmp_path / "b.json", runner=_fake_runner(seen=seen))
    b.start("a small calculator app", "calc")
    _wait(b)
    argv = seen[0][0]
    assert argv[0] == "/usr/bin/bwrap" and "--unshare-all" in argv and "--share-net" in argv
    home = Path.home()
    ro = [argv[i + 1] for i, a in enumerate(argv) if a in ("--ro-bind", "--ro-bind-try")]
    for name in ("settings.json", "hooks", "skills", "plugins", "CLAUDE.md"):
        assert str(home / ".claude" / name) in ro
    rw = [argv[i + 1] for i, a in enumerate(argv) if a in ("--bind", "--bind-try")]
    assert str(tmp_path / "P" / "calc") in rw
    assert not any(p.startswith(str(home / "NOVA")) or p.endswith(".ssh") or "gh" in Path(p).parts for p in rw + ro)
    # ~/.claude.json inside is a throwaway copy, never the real file
    i = argv.index(str(home / ".claude.json"))
    assert argv[i - 2] == "--bind" and argv[i - 1] != str(home / ".claude.json")


def test_one_build_at_a_time(tmp_path):
    gate = threading.Event()
    b = Builder(projects=tmp_path / "P", state=tmp_path / "b.json", runner=_fake_runner(gate=gate), sandbox=False)
    b.start("first project to build", "one")
    with pytest.raises(ValueError):
        b.start("second project to build", "two")
    gate.set()
    _wait(b)


def test_a_failed_build_says_so(tmp_path):
    b = Builder(projects=tmp_path / "P", state=tmp_path / "b.json", runner=_fake_runner(code=1), sandbox=False)
    b.start("something that will fail", "oops")
    assert _wait(b)["status"] == "failed"


def test_a_restart_marks_an_unfinished_build_failed(tmp_path):
    state = tmp_path / "b.json"
    state.write_text(json.dumps([{"id": "1", "request": "x" * 10, "slug": "x", "status": "building"}]))
    b = Builder(projects=tmp_path / "P", state=state, sandbox=False)
    b.recover()
    assert b.list()[0]["status"] == "failed"


def test_a_symlinked_project_folder_is_refused(tmp_path):
    (tmp_path / "P").mkdir()
    (tmp_path / "elsewhere").mkdir()
    try:
        (tmp_path / "P" / "evil").symlink_to(tmp_path / "elsewhere")
    except OSError:
        pytest.skip("no symlink rights here (Windows); the hub is Linux")
    b = Builder(projects=tmp_path / "P", state=tmp_path / "b.json", runner=_fake_runner(), sandbox=False)
    with pytest.raises(ValueError):
        b.start("anything at all here", "evil")


async def test_build_skills_through_the_hub(tmp_path):
    import nova.skills  # noqa: F401
    from nova.brain import hub_tier
    from nova.config import Config
    from nova.security.audit import AuditLog
    from nova.security.ladder import Ladder
    from nova.skills.registry import SkillCtx, get_skill

    class _M:
        model = "m"

    b = Builder(projects=tmp_path / "P", state=tmp_path / "b.json", runner=_fake_runner(), sandbox=False)
    svc = HubService(model=_M(), data_dir=tmp_path, builder=b)
    hub_tier.use_local_service(svc)
    try:
        ctx = SkillCtx(config=Config(), ladder=Ladder(AuditLog(tmp_path / "a.log")))
        reply = await get_skill("build_project").handler(ctx, request="a portfolio website", name="site")
        assert "Max plan" in reply.text and "H:\\Projects" in reply.text
        _wait(b)
        status = await get_skill("build_status").handler(ctx)
        assert "site is done" in status.text and "Open index.html" in status.text
    finally:
        hub_tier.use_local_service(None)


# --- the face ------------------------------------------------------------------------------

def test_face_moves_through_a_conversation():
    now = [1000.0]
    f = Face(clock=lambda: now[0])
    assert f.state()["mode"] == "idle"
    f.thinking("your laptop", "what's the weather?")
    s = f.state()
    assert s["mode"] == "thinking" and s["heard"] == "what's the weather?" and s["source"] == "your laptop"
    f.speaking("Sunny and 72.")
    assert f.state()["mode"] == "speaking"
    now[0] += 10
    assert f.state()["mode"] == "idle" and f.state()["said"] == "Sunny and 72."  # words stay, motion stops


def test_talk_drives_the_face_but_never_with_unaddressed_speech():
    face = Face()
    talk = Talk(_Says("Done."), None, face)
    assert talk.hear_text("laptop", "my bank pin is 1234 and anyway", want_audio=False) == {"ignored": True}
    assert face.state()["heard"] == "" and face.state()["seq"] == 0
    talk.hear_text("laptop", "Nova, mute", want_audio=False)
    s = face.state()
    assert s["heard"] == "mute" and s["said"] == "Done." and s["source"] == "your laptop"


@pytest.fixture
def hub(tmp_path):
    from nova.hub.listener import make_server

    (tmp_path / "hub_owner").write_text("manav@example.com\n")
    (tmp_path / "hub_name").write_text("nova-hub.tail1.ts.net\n")

    class _M:
        model = "m"

    face = Face()
    face.thinking("the phone", "secret question")
    server = make_server(HubService(model=_M(), data_dir=tmp_path, face=face), 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server.server_address
    server.shutdown()
    server.server_close()


def _get(addr, path, **headers):
    req = urllib.request.Request(f"http://{addr[0]}:{addr[1]}{path}")
    for k, v in headers.items():
        req.add_header(k.replace("_", "-"), v)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read().decode(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(), {}


def test_the_hubs_own_screen_and_the_owner_see_the_face(hub):
    status, body, _ = _get(hub, "/v1/face/state", Host=f"127.0.0.1:{hub[1]}")
    assert status == 200 and json.loads(body)["heard"] == "secret question"
    status, page, headers = _get(hub, "/face", Host=f"127.0.0.1:{hub[1]}")
    csp = {k.lower(): v for k, v in headers.items()}["content-security-policy"]
    assert status == 200 and "<canvas" in page and "__NONCE__" not in page and "connect-src 'self'" in csp
    assert _get(hub, "/v1/face/state", Host="nova-hub.tail1.ts.net", Tailscale_User_Login="manav@example.com")[0] == 200


@pytest.mark.parametrize("headers", [
    {"Host": "nova-hub.tail1.ts.net"},                                                   # a tailnet device with no user
    {"Host": "nova-hub.tail1.ts.net", "Tailscale_User_Login": "friend@example.com"},     # someone else
    {"Host": "evil.example:8765"},                                                        # DNS rebinding
    {"Host": "nova-hub.tail1.ts.net:8443", "Tailscale_Funnel_Request": "?1"},            # the internet
    {"Host": "127.0.0.1:{port}", "Sec_Fetch_Site": "cross-site"},                        # a web page on the hub
])
def test_nobody_else_sees_what_was_said(hub, headers):
    headers = {k: v.replace("{port}", str(hub[1])) for k, v in headers.items()}
    status, body, _ = _get(hub, "/v1/face/state", **headers)
    assert status in (403, 404) and "secret" not in body


# --- the face as the hub's desktop background (ADR 0028) ----------------------

_REPO = Path(__file__).resolve().parents[1]
_FACE = _REPO / "hub" / "face"


def test_the_background_app_shows_the_hubs_own_face_page():
    src = (_FACE / "renderer.py").read_text(encoding="utf-8")
    assert 'URL = "http://127.0.0.1:8765/face"' in src   # loopback only: the face's own-screen access rule
    assert "load-failed" in src                          # comes back when the service restarts


def test_the_extension_and_the_app_agree_on_the_window():
    src = (_FACE / "renderer.py").read_text(encoding="utf-8")
    ext = (_FACE / "nova-face@nova" / "extension.js").read_text(encoding="utf-8")
    assert 'TITLE = "NOVA face"' in src and "const TITLE = 'NOVA face';" in ext
    assert 'APP_ID = "io.nova.face"' in src and "'io.nova.face'" in ext
    assert "_backgroundGroup" in ext and "disable()" in ext


def test_the_extension_metadata_matches_its_folder():
    meta = json.loads((_FACE / "nova-face@nova" / "metadata.json").read_text(encoding="utf-8"))
    assert meta["uuid"] == "nova-face@nova"
    assert all(v.isdigit() for v in meta["shell-version"])


def test_face_on_uses_the_background_app_not_a_browser():
    script = (_REPO / "hub" / "nova-hub").read_text(encoding="utf-8")
    body = script[script.index("cmd_face() {"):script.index("cmd_logs() {")]
    assert "hub/face/renderer.py" in body and "face_extension_on" in body
    assert "--kiosk" not in script
    assert "(y/n)" in body        # apt install and auto sign-in are both asked
