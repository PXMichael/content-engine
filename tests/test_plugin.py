"""Tests for the Content Engine plugin registration and scoring logic."""

import importlib.util
import json
import os
import sys
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_DIR))


def _load_tools():
    spec = importlib.util.spec_from_file_location("ce_tools", PLUGIN_DIR / "tools.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_engine():
    spec = importlib.util.spec_from_file_location("ce_engine", PLUGIN_DIR / "engine.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_registers_5_tools():
    mod = _load_tools()
    registered = []

    class FakeCtx:
        def register_tool(self, name, toolset, schema, handler, check_fn=None, emoji=None):
            registered.append(name)
            assert name == schema["name"]

    mod.register_tools(FakeCtx())
    assert set(registered) == {
        "ce_fetch_transcript",
        "ce_score_moments",
        "ce_write_copy",
        "ce_cut_clips",
        "ce_run",
    }


def test_engine_constants():
    eng = _load_engine()
    src = (PLUGIN_DIR / "engine.py").read_text(encoding="utf-8")
    assert "services.leadconnectorhq.com" not in src
    assert "NvqL18HRoYJ29fAGdWSH" not in src


def test_score_moments_returns_non_overlapping():
    eng = _load_engine()
    segments = []
    for i in range(0, 600, 5):
        segments.append({"start": float(i), "duration": 5.0, "filler": True,
                         "text": f"Segment number {i // 5} with a fact about 1947 and a name like John Smith."})
    clips = eng.score_moments(segments, min_duration=45, max_duration=60, count=3)
    assert len(clips) <= 3
    for c in clips:
        assert 45 <= c["duration"] <= 60
        assert 0 < c["total"] <= 21
    for i, a in enumerate(clips):
        for b in clips[i + 1:]:
            assert a["end"] <= b["start"] or a["start"] >= b["end"]


def test_write_copy_covers_platforms():
    eng = _load_engine()
    text = "The 1947 Roswell incident was not a weather balloon. John Smith saw the craft. It changed everything."
    out = eng.write_copy(text, platforms=["x", "facebook", "shorts", "tiktok"], url="https://youtu.be/abc")
    assert set(out.keys()) == {"x", "facebook", "shorts", "tiktok"}
    assert "Link in bio" in out["tiktok"]
    assert "https://youtu.be/abc" in out["x"]
    assert "https://youtu.be/abc" not in out["shorts"]


def test_run_handler_requires_url():
    mod = _load_tools()
    out = json.loads(mod._handle_run({}))
    assert "error" in out


def test_url_validation_accepts_youtube_hosts():
    eng = _load_engine()
    for u in (
        "https://youtu.be/q-OfppGbeXY",
        "https://www.youtube.com/watch?v=aircAruvnKk",
        "https://youtube.com/watch?v=aircAruvnKk",
        "https://m.youtube.com/watch?v=aircAruvnKk",
    ):
        assert eng.validate_video_url(u) == u


def test_url_validation_rejects_option_injection_and_other_hosts():
    eng = _load_engine()
    bad = [
        "--exec=whoami",
        "-https://youtu.be/abc",
        "https://youtu.be.evil.example/abc",
        "http://youtu.be/abc",
        "169.254.169.254/latest/meta-data",
        "https://169.254.169.254/",
        "https://youtube.com:8080@evil.example/",
        "https://user:pass@youtu.be/abc",
    ]
    for u in bad:
        try:
            eng.validate_video_url(u)
            raise AssertionError(f"should have rejected: {u}")
        except ValueError:
            pass


def test_out_dir_confined_to_plugin_work_dir():
    eng = _load_engine()
    base = eng._plugin_work_dir()
    ok = eng._resolve_out_dir("demo-run")
    assert str(base).replace("\\", "/") in ok.replace("\\", "/")
    for bad in ("../../escape", "/etc", "C:/Windows/Temp"):
        try:
            eng._resolve_out_dir(bad)
            raise AssertionError(f"should have rejected out_dir: {bad}")
        except ValueError:
            pass


def test_label_and_filter_escaping():
    eng = _load_engine()
    assert eng._safe_label("clip_C03") == "clip_C03"
    for bad in ("../evil", "a/b", "clip;something", "with space"):
        try:
            eng._safe_label(bad)
            raise AssertionError(f"should have rejected label: {bad}")
        except ValueError:
            pass
    assert eng._escape_filter_path("a'b:c") == "a\\'b\\:c"


def test_no_hardcoded_personal_paths():
    src = (PLUGIN_DIR / "engine.py").read_text(encoding="utf-8")
    assert "C:/Users/" not in src
    assert "shutil.which" in src
