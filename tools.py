"""Content Engine tools for Hermes Agent.

Five tools that turn a long YouTube video into scored clips and per-platform
social copy. The heavy lifting lives in engine.py; this module holds the
schemas and the JSON envelope.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from typing import Any, Dict, List, Optional

try:  # package import when Hermes loads the plugin
    from . import engine
except ImportError:  # file-path import when tests load tools.py directly
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import engine

logger = logging.getLogger(__name__)

TOOLSET = "content-engine"


def tool_error(message: str) -> str:
    return json.dumps({"error": str(message)}, ensure_ascii=False)


def _dispatch(fn, log_name: str, fail_msg: str) -> str:
    try:
        result = fn()
        return json.dumps({"result": result}, ensure_ascii=False)
    except Exception as e:
        logger.error("%s error: %s", log_name, e)
        return tool_error(f"{fail_msg}: {e}")


def _load_segments(args: dict) -> List[Dict[str, Any]]:
    """Load segments from an inline list or a transcript JSON path."""
    if args.get("segments"):
        return list(args["segments"])
    path = args.get("transcript_path") or args.get("path")
    if not path:
        raise RuntimeError("Missing required parameter: transcript_path or segments")
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, dict):
        data = data.get("segments") or []
    return data


# --- Handlers ---

def _handle_fetch_transcript(args: dict, **kw) -> str:
    url = (args.get("url") or args.get("youtube_url") or "").strip()
    if not url:
        return tool_error("Missing required parameter: url")
    out_dir = args.get("out_dir") or None

    def run():
        t = engine.fetch_transcript(url, out_dir=out_dir)
        return {
            "video_id": t["video_id"],
            "title": t["title"],
            "channel": t["channel"],
            "duration": t["duration"],
            "segment_count": t["segment_count"],
            "transcript_path": t["transcript_path"],
            "transcript_txt": t["transcript_txt"],
            "work_dir": t["work_dir"],
            "preview": t["segments"][:5],
        }
    return _dispatch(run, "ce_fetch_transcript", "Failed to fetch transcript")


def _handle_score_moments(args: dict, **kw) -> str:
    def run():
        segments = _load_segments(args)
        clips = engine.score_moments(
            segments,
            min_duration=float(args.get("min_duration", 45)),
            max_duration=float(args.get("max_duration", 60)),
            count=int(args.get("count", 6)),
            stride=float(args.get("stride", 60)),
        )
        return [
            {
                "id": c["id"],
                "start": c["start"],
                "end": c["end"],
                "duration": c["duration"],
                "scores": c["scores"],
                "total": c["total"],
                "text": c["text"][:600],
            }
            for c in clips
        ]
    return _dispatch(run, "ce_score_moments", "Failed to score moments")


def _handle_write_copy(args: dict, **kw) -> str:
    clip_text = args.get("clip_text") or args.get("text") or ""
    if not clip_text:
        return tool_error("Missing required parameter: clip_text")
    platforms = args.get("platforms") or ["x", "facebook", "shorts", "tiktok"]
    if isinstance(platforms, str):
        platforms = [p.strip() for p in platforms.split(",") if p.strip()]

    def run():
        return engine.write_copy(
            clip_text,
            platforms=platforms,
            url=args.get("url") or "",
            title=args.get("title") or "",
        )
    return _dispatch(run, "ce_write_copy", "Failed to write copy")


def _handle_cut_clips(args: dict, **kw) -> str:
    video_path = args.get("video_path") or args.get("source") or ""
    if not video_path:
        return tool_error("Missing required parameter: video_path")
    clips = args.get("clips") or []
    if not clips:
        return tool_error("Missing required parameter: clips")
    out_dir = args.get("out_dir") or None
    segments = args.get("segments") or None

    def run():
        return engine.cut_clips(
            video_path,
            clips,
            out_dir=out_dir,
            segments=segments,
            subs=bool(args.get("subs", True)),
        )
    return _dispatch(run, "ce_cut_clips", "Failed to cut clips")


def _handle_run(args: dict, **kw) -> str:
    url = (args.get("url") or args.get("youtube_url") or "").strip()
    if not url:
        return tool_error("Missing required parameter: url")
    platforms = args.get("platforms") or ["x", "facebook", "shorts", "tiktok"]
    if isinstance(platforms, str):
        platforms = [p.strip() for p in platforms.split(",") if p.strip()]

    def run():
        return engine.run(
            url,
            count=int(args.get("count", 6)),
            platforms=platforms,
            out_dir=args.get("out_dir") or None,
            min_duration=float(args.get("min_duration", 45)),
            max_duration=float(args.get("max_duration", 60)),
        )
    return _dispatch(run, "ce_run", "Failed to run the pipeline")


# --- Schemas ---

FETCH_TRANSCRIPT_SCHEMA = {
    "name": "ce_fetch_transcript",
    "description": "Fetch the timestamped transcript and metadata for a YouTube video. Returns the transcript path and a preview.",
    "parameters": {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "YouTube watch, share, or Shorts URL."},
            "out_dir": {"type": "string", "description": "Optional working directory for the transcript files."},
        },
        "required": ["url"],
    },
}

SCORE_MOMENTS_SCHEMA = {
    "name": "ce_score_moments",
    "description": "Score transcript segments and return the top non-overlapping 45-60 second clip candidates.",
    "parameters": {
        "type": "object",
        "properties": {
            "transcript_path": {"type": "string", "description": "Path to transcript.json from ce_fetch_transcript."},
            "segments": {"type": "array", "description": "Inline transcript segments, instead of a path."},
            "min_duration": {"type": "number", "description": "Minimum clip length in seconds. Default 45."},
            "max_duration": {"type": "number", "description": "Maximum clip length in seconds. Default 60."},
            "count": {"type": "integer", "description": "How many clips to return. Default 6."},
            "stride": {"type": "number", "description": "Candidate spacing in seconds. Default 60."},
        },
        "required": [],
    },
}

WRITE_COPY_SCHEMA = {
    "name": "ce_write_copy",
    "description": "Write a first draft of per-platform social copy from a clip transcript.",
    "parameters": {
        "type": "object",
        "properties": {
            "clip_text": {"type": "string", "description": "The clip transcript text."},
            "platforms": {"type": "array", "items": {"type": "string"}, "description": "x, facebook, shorts, tiktok, instagram. Default all four."},
            "url": {"type": "string", "description": "Link to include where the platform allows it."},
            "title": {"type": "string", "description": "Optional video title."},
        },
        "required": ["clip_text"],
    },
}

CUT_CLIPS_SCHEMA = {
    "name": "ce_cut_clips",
    "description": "Cut clips from a downloaded video with ffmpeg, burning in subtitles when a transcript is provided.",
    "parameters": {
        "type": "object",
        "properties": {
            "video_path": {"type": "string", "description": "Path to the source mp4."},
            "clips": {"type": "array", "description": "List of {label, orientation (h|v), start, end}."},
            "out_dir": {"type": "string", "description": "Optional output directory."},
            "segments": {"type": "array", "description": "Transcript segments for subtitle timing."},
            "subs": {"type": "boolean", "description": "Burn subtitles. Default true."},
        },
        "required": ["video_path", "clips"],
    },
}

RUN_SCHEMA = {
    "name": "ce_run",
    "description": "Run the full Content Engine pipeline on a YouTube URL: fetch transcript, score moments, and draft per-platform copy.",
    "parameters": {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "YouTube watch, share, or Shorts URL."},
            "count": {"type": "integer", "description": "How many clips to return. Default 6."},
            "platforms": {"type": "array", "items": {"type": "string"}, "description": "x, facebook, shorts, tiktok, instagram. Default all four."},
            "out_dir": {"type": "string", "description": "Optional working directory."},
            "min_duration": {"type": "number", "description": "Minimum clip length in seconds. Default 45."},
            "max_duration": {"type": "number", "description": "Maximum clip length in seconds. Default 60."},
        },
        "required": ["url"],
    },
}


TOOLS = (
    (FETCH_TRANSCRIPT_SCHEMA, _handle_fetch_transcript),
    (SCORE_MOMENTS_SCHEMA, _handle_score_moments),
    (WRITE_COPY_SCHEMA, _handle_write_copy),
    (CUT_CLIPS_SCHEMA, _handle_cut_clips),
    (RUN_SCHEMA, _handle_run),
)


def register_tools(ctx) -> None:
    """Register all Content Engine tools in the content-engine toolset."""
    for schema, handler in TOOLS:
        ctx.register_tool(
            name=schema["name"],
            toolset=TOOLSET,
            schema=schema,
            handler=handler,
            emoji="🎬",
        )
