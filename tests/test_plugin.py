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
