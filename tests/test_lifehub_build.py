"""The life hub's builder (deploy/lifehub/build.py) with a fake Hugo: a release goes live whole and checked, or not
at all; the gate finds what the page policy would block; the router reads how the last build went."""

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("lifehub_build", ROOT / "deploy" / "lifehub" / "build.py")
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)

FAKE_HUGO = """#!{python}
import json, os, sys, time
args = sys.argv[1:]
source, out = args[args.index("--source") + 1], args[args.index("--destination") + 1]
now = json.load(open(os.path.join(source, "assets", "now.json")))
if now.get("fail"):
    print(now["fail"], file=sys.stderr)
    sys.exit(1)
time.sleep(now.get("sleep", 0))
os.makedirs(os.path.join(out, "css"), exist_ok=True)
open(os.path.join(out, "index.html"), "w").write(now.get("page", "<p>Сейчас</p>"))
open(os.path.join(out, "css", "lifehub.css"), "w").write("body{{}}")
"""


@pytest.fixture
def site(tmp_path, monkeypatch):
    fake = tmp_path / "hugo"
    fake.write_text(FAKE_HUGO.format(python=sys.executable))
    fake.chmod(0o755)
    source = tmp_path / "source"
    (source / "layouts").mkdir(parents=True)
    for name, value in (("DATA", tmp_path / "data"), ("SITE", tmp_path / "site"), ("SOURCE", source),
                        ("HUGO", str(fake)), ("WORK", tmp_path / "work" / "lifehub")):
        monkeypatch.setattr(builder, name, value)
    (tmp_path / "data").mkdir()
    return tmp_path


def data(root, **now):
    (root / "data" / "now.json").write_text(json.dumps(now))


def test_a_release_goes_live_whole_and_a_bad_one_never_does(site):
    state = {}
    data(site, page="<p>Сейчас</p>")
    assert builder.step(state, 1_760_000_000) is True
    current = site / "site" / "current"
    first = os.readlink(current)
    assert first.startswith("releases/20251009T085320Z-") and not first.startswith("/"), "relative: nginx mounts it"
    assert (current / "index.html").read_text() == "<p>Сейчас</p>"
    built = json.loads((site / "site" / "build.json").read_text())
    assert built == {"ok": True, "at": 1_760_000_000, "error": "", "release": first.removeprefix("releases/")}
    assert builder.step(state, 1_760_000_015) is False, "the same data: nothing to build"

    data(site, page='<p onclick="x()">Сейчас</p><script>alert(1)</script>')
    assert builder.step(state, 1_760_000_030) is False
    built = json.loads((site / "site" / "build.json").read_text())
    assert not built["ok"] and built["error"] == "проверка страниц: index.html: p[onclick]; index.html: <script>"
    assert os.readlink(current) == first and (current / "index.html").read_text() == "<p>Сейчас</p>"
    assert not [p for p in (site / "site" / "releases").iterdir() if p.name.endswith(".tmp")], "no half release"

    data(site, fail="ERROR render of \"home\" failed: home.html:3:1: nil pointer")
    builder.step(state, 1_760_000_045)
    assert json.loads((site / "site" / "build.json").read_text())["error"] == (
        'Hugo: ERROR render of "home" failed: home.html:3:1: nil pointer')
    assert builder.step(state, 1_760_000_060) is False, "a failed build is not repeated every 15 s"
    for n in range(4):
        data(site, page=f"<p>{n}</p>")
        builder.step(state, 1_760_000_100 + n)
    assert len(list((site / "site" / "releases").iterdir())) == builder.KEEP
    assert (current / "index.html").read_text() == "<p>3</p>"


def test_hugo_that_hangs_is_stopped_and_the_old_release_stays(site, monkeypatch):
    monkeypatch.setattr(builder, "HUGO_TIMEOUT_S", 1)
    data(site, page="<p>a</p>")
    builder.step({}, 1_760_000_000)
    data(site, sleep=5)
    builder.step({"hash": "x"}, 1_760_000_100)
    built = json.loads((site / "site" / "build.json").read_text())
    assert built["error"] == "Hugo не уложился в 1 с" and (site / "site" / "current" / "index.html").exists()


def test_the_gate_finds_what_the_policy_would_block_or_that_runs(tmp_path):
    release = tmp_path / "release"
    (release / "trips" / "a").mkdir(parents=True)
    (release / "charts").mkdir()
    (release / "photos").mkdir()
    (release / "index.html").write_text('<link rel="stylesheet" href="/css/lifehub.css"><a href="https://kiwi.com/x">'
                                        'https://kiwi.com/x</a><img src="/photos/1.jpg" alt="">')
    (release / "trips" / "a" / "index.html").write_text(
        '<a href="javascript:alert(1)">x</a><img src="https://evil.example/p.png"><div style="color:red"></div>'
        '<link rel="stylesheet" href="https://fonts.example/x.css"><iframe src="/x"></iframe>')
    (release / "charts" / "t.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg"><script>x</script>'
                                              '<rect onload="x" width="1"/></svg>')
    (release / "photos" / "1.jpg").write_bytes(b"\xff\xd8\xff\xe0 jpeg")
    (release / "photos" / "2.jpg").write_bytes(b"<svg onload=alert(1)>")
    (release / "app.js").write_text("alert(1)")
    assert builder.gate(release) == [
        "app.js: такого файла в хабе быть не должно",
        "charts/t.svg: <script>", "charts/t.svg: rect[onload]",
        "photos/2.jpg: не JPEG",
        "trips/a/index.html: a[href=javascript:alert(1)]",
        "trips/a/index.html: img[src] from elsewhere: https://evil.example/p.png",
        "trips/a/index.html: div[style]", "trips/a/index.html: <link>",
        "trips/a/index.html: link[href] from elsewhere: https://fonts.example/x.css", "trips/a/index.html: <iframe>"]


def test_the_router_says_how_the_last_build_went(tmp_path):
    """build.json in the health line: when the hub was built, or why it is not up to date."""
    from retinue import lifehub
    from retinue.archive import Archive
    from retinue.core import Core
    from retinue.protocol import Store

    from test_core import AGENT

    built = tmp_path / "site" / "build.json"
    hub = lifehub.Lifehub(lifehub.Data(str(tmp_path / "data")), "https://hub.in.example.com", [],
                          build_status=str(built))
    core = Core([AGENT], Store(str(tmp_path / "r.sqlite")), "owner", archive=Archive(":memory:"), lifehub=hub)
    now = 1_760_000_000
    assert core.health(now).endswith(", хаб ещё не собран."), "no file yet: the builder has not run"
    built.parent.mkdir()
    built.write_text(json.dumps({"ok": True, "at": now - 120, "error": "", "release": "r"}))
    assert core.health(now).endswith(", хаб собран 2 мин назад.")
    built.write_text(json.dumps({"ok": False, "at": now - 60, "error": "Hugo: ERROR x", "release": "r"}))
    assert core.health(now).endswith(", хаб не обновлён: Hugo: ERROR x.")
    built.write_text("{torn")
    assert core.health(now).endswith(", статус хаба не читается.")
    assert {"name": "хаб", "ok": False, "state": "статус хаба не читается"} in hub.connectors(now)
