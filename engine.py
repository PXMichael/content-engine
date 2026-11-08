"""Content Engine core.

Fetch a YouTube transcript, score clip moments, write per-platform social
copy, and cut clips with ffmpeg. No API keys, no model tokens. Uses yt-dlp
for transcript download and ffmpeg for cutting, both discovered on PATH or
in the standard Hermes locations.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import time
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# Tool discovery
# ---------------------------------------------------------------------------

_YT_DLP_CANDIDATES = [
    "yt-dlp",
    "C:/Users/micha/AppData/Local/hermes/hermes-agent/venv/Scripts/yt-dlp.exe",
    "C:/Users/micha/.local/bin/yt-dlp",
]

_FFMPEG_CANDIDATES = [
    "ffmpeg",
    "C:/Users/micha/AppData/Local/Microsoft/WinGet/Packages/Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe/ffmpeg-8.1-full_build/bin/ffmpeg.exe",
]


def _find_tool(candidates: List[str]) -> Optional[str]:
    for cand in candidates:
        if not cand:
            continue
        try:
            subprocess.run(
                [cand, "-version"] if "ffmpeg" in cand else [cand, "--version"],
                capture_output=True, timeout=15,
            )
            return cand
        except Exception:
            continue
    return None


def _ytdlp() -> str:
    path = _find_tool(_YT_DLP_CANDIDATES)
    if not path:
        raise RuntimeError(
            "yt-dlp was not found. Install yt-dlp and put it on PATH, or place it "
            "at the Hermes venv Scripts directory."
        )
    return path


def _ffmpeg() -> str:
    path = _find_tool(_FFMPEG_CANDIDATES)
    if not path:
        raise RuntimeError(
            "ffmpeg was not found. Install ffmpeg and put it on PATH, or place it "
            "at the standard WinGet package location."
        )
    return path


# ---------------------------------------------------------------------------
# Transcript
# ---------------------------------------------------------------------------

_VIDEO_ID_RE = re.compile(r"(?:v=|youtu\.be/|/shorts/|/embed/)([A-Za-z0-9_-]{11})")


def video_id(url: str) -> str:
    m = _VIDEO_ID_RE.search(url)
    return m.group(1) if m else url.strip()


def _parse_vtt(path: str) -> List[Dict[str, Any]]:
    with open(path, encoding="utf-8") as f:
        content = f.read()
    segments: List[Dict[str, Any]] = []
    for block in re.split(r"\n\s*\n", content):
        lines = block.strip().split("\n")
        if len(lines) < 2:
            continue
        timing_idx = None
        for i, line in enumerate(lines):
            if "-->" in line:
                timing_idx = i
                break
        if timing_idx is None:
            continue
        m = re.match(
            r"(\d+):(\d+):([\d.]+)\s*-->\s*(\d+):(\d+):([\d.]+)",
            lines[timing_idx].strip(),
        )
        if not m:
            continue
        h1, m1, s1, h2, m2, s2 = m.groups()
        start = int(h1) * 3600 + int(m1) * 60 + float(s1)
        end = int(h2) * 3600 + int(m2) * 60 + float(s2)
        text = " ".join(lines[timing_idx + 1:]).strip()
        text = re.sub(r"<[^>]+>", "", text)
        text = text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
        if text:
            segments.append({"start": round(start, 2), "duration": round(end - start, 2), "text": text})
    return segments


def fetch_transcript(url: str, out_dir: Optional[str] = None) -> Dict[str, Any]:
    """Download the transcript and metadata for a YouTube video.

    Returns {video_id, title, channel, duration, segment_count, transcript_path,
    transcript_txt, segments}.
    """
    yt = _ytdlp()
    out_dir = out_dir or tempfile.mkdtemp(prefix="content-engine-")
    os.makedirs(out_dir, exist_ok=True)
    vid = video_id(url)

    meta = subprocess.run(
        [yt, "--skip-download", "--print", "%(title)s", "--print", "%(channel)s",
         "--print", "%(duration_string)s", url],
        capture_output=True, text=True, timeout=120,
    )
    if meta.returncode != 0:
        raise RuntimeError(f"yt-dlp metadata failed: {meta.stderr[-500:]}")
    lines = [l for l in meta.stdout.strip().split("\n") if l.strip()]
    title = lines[0] if len(lines) > 0 else ""
    channel = lines[1] if len(lines) > 1 else ""
    duration = lines[2] if len(lines) > 2 else ""

    sub = subprocess.run(
        [yt, "--skip-download", "--write-subs", "--write-auto-subs", "--sub-lang", "en",
         "--sub-format", "vtt", "-o", os.path.join(out_dir, "transcript"), url],
        capture_output=True, text=True, timeout=300,
    )
    vtt_files = sorted(f for f in os.listdir(out_dir) if f.endswith(".vtt"))
    if not vtt_files:
        raise RuntimeError(
            "No transcript found. The video may have no captions, or yt-dlp "
            f"could not fetch them. yt-dlp said: {sub.stderr[-300:]}"
        )

    segments = _parse_vtt(os.path.join(out_dir, vtt_files[0]))
    if not segments:
        raise RuntimeError("Transcript file was empty or unreadable.")

    transcript_path = os.path.join(out_dir, "transcript.json")
    with open(transcript_path, "w", encoding="utf-8") as f:
        json.dump(segments, f, ensure_ascii=False, indent=2)

    txt_lines = [
        f"{int(s['start'] // 60):02d}:{int(s['start'] % 60):02d} | {s['text']}"
        for s in segments
    ]
    txt_path = os.path.join(out_dir, "transcript.txt")
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write("\n".join(txt_lines))

    return {
        "video_id": vid,
        "title": title,
        "channel": channel,
        "duration": duration,
        "segment_count": len(segments),
        "transcript_path": transcript_path,
        "transcript_txt": txt_path,
        "work_dir": out_dir,
        "segments": segments,
    }


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

_HOOK_KEYWORDS = [
    "secret", "never", "first", "evidence", "proof", "hidden", "revealed",
    "unknown", "mystery", "discovered", "actually", "real", "truth", "shocking",
    "nobody", "everyone", "always", "nothing", "everything",
]
_VISUAL_KEYWORDS = [
    "look", "see", "show", "image", "photo", "video", "footage", "picture",
    "screen", "graphic", "watch", "view", "display", "frame", "camera",
]
_NUMBER_RE = re.compile(r"\b\d[\d,.]*\b")
_NAME_RE = re.compile(r"\b[A-Z][a-z]{2,}(?:\s+[A-Z][a-z]{2,})+\b")


def _make_candidates(segments: List[Dict[str, Any]], lo: float, hi: float, stride: float) -> List[Dict[str, Any]]:
    """Segment-aligned candidate windows, ported from ClipBot 5000."""
    cands: List[Dict[str, Any]] = []
    i = 0
    n = len(segments)
    while i < n:
        start = segments[i]["start"]
        j = i
        while j < n and (segments[j]["start"] + segments[j]["duration"] - start) < lo:
            j += 1
        best_j = None
        while j < n and (segments[j]["start"] + segments[j]["duration"] - start) <= hi:
            best_j = j
            j += 1
        if best_j is not None:
            end = segments[best_j]["start"] + segments[best_j]["duration"]
            text = " ".join(s["text"] for s in segments[i:best_j + 1])
            cands.append({
                "start": round(start, 2),
                "end": round(end, 2),
                "duration": round(end - start, 2),
                "text": text,
                "first": segments[i]["text"].strip(),
                "last": segments[best_j]["text"].strip(),
            })
        target = start + stride
        while i < n and segments[i]["start"] < target:
            i += 1
        if i >= n:
            break
    out: List[Dict[str, Any]] = []
    seen = set()
    for c in cands:
        key = round(c["start"], 0)
        if key in seen:
            continue
        seen.add(key)
        out.append(c)
    for idx, c in enumerate(out, 1):
        c["id"] = f"C{idx:02d}"
    return out


def _score_candidate(text: str, first: str, duration: float) -> Dict[str, int]:
    """Deterministic 1-5 scoring on four criteria, max 20."""
    first_lower = first.lower()
    text_lower = text.lower()

    hook = 1
    if _NUMBER_RE.search(first):
        hook += 1
    if any(w in first_lower for w in _HOOK_KEYWORDS):
        hook += 1
    if _NAME_RE.search(first):
        hook += 1
    if len(first.split()) >= 8:
        hook += 1
    hook = min(hook, 5)

    completeness = 2
    words = len(text.split())
    if words >= 40:
        completeness += 1
    if words >= 80:
        completeness += 1
    if any(w in text_lower for w in ["because", "so", "therefore", "which means"]):
        completeness += 1
    completeness = min(completeness, 5)

    standalone = 2
    if not re.search(r"\b(this|that|it|they|them|he|she)\b", first_lower):
        standalone += 1
    if _NUMBER_RE.search(text):
        standalone += 1
    if _NAME_RE.search(text):
        standalone += 1
    standalone = min(standalone, 5)

    visual = 1
    if any(w in text_lower for w in _VISUAL_KEYWORDS):
        visual += 2
    if _NUMBER_RE.search(text):
        visual += 1
    if any(w in text_lower for w in ["here", "below", "above", "left", "right"]):
        visual += 1
    visual = min(visual, 5)

    length_bonus = 0
    if 45 <= duration <= 65:
        length_bonus = 1

    total = hook + completeness + standalone + visual + length_bonus
    return {
        "hook": hook,
        "completeness": completeness,
        "standalone": standalone,
        "visual": visual,
        "length_bonus": length_bonus,
        "total": total,
    }


def score_moments(
    segments: List[Dict[str, Any]],
    min_duration: float = 45,
    max_duration: float = 60,
    count: int = 6,
    stride: float = 60,
) -> List[Dict[str, Any]]:
    """Generate candidates, score them, and return the top N non-overlapping."""
    cands = _make_candidates(segments, min_duration, max_duration, stride)
    for c in cands:
        c["scores"] = _score_candidate(c["text"], c["first"], c["duration"])
        c["total"] = c["scores"]["total"]
    cands.sort(key=lambda x: x["total"], reverse=True)

    picked: List[Dict[str, Any]] = []
    for c in cands:
        overlap = False
        for p in picked:
            if not (c["end"] <= p["start"] or c["start"] >= p["end"]):
                overlap = True
                break
        if overlap:
            continue
        picked.append(c)
        if len(picked) >= count:
            break
    return picked


# ---------------------------------------------------------------------------
# Copy
# ---------------------------------------------------------------------------

def _first_sentence(text: str) -> str:
    for sep in [". ", "? ", "! "]:
        idx = text.find(sep)
        if idx > 0:
            return text[: idx + 1].strip()
    return text[:140].strip()


def _key_fact(text: str) -> str:
    sentences = re.split(r"(?<=[.!?])\s+", text)
    best = ""
    for s in sentences:
        if _NUMBER_RE.search(s) or _NAME_RE.search(s):
            if len(s) > len(best):
                best = s.strip()
    return best[:200] if best else text[:140].strip()


def write_copy(
    clip_text: str,
    platforms: Optional[List[str]] = None,
    url: str = "",
    title: str = "",
) -> Dict[str, str]:
    """Deterministic first-draft social copy per platform."""
    platforms = platforms or ["x", "facebook", "shorts", "tiktok"]
    hook = _first_sentence(clip_text)
    fact = _key_fact(clip_text)
    out: Dict[str, str] = {}
    for p in platforms:
        p = p.lower()
        if p == "x":
            out[p] = f"{hook} {fact} {url}".strip()
        elif p == "facebook":
            out[p] = f"{hook}\n\n{fact}\n\n{url}".strip()
        elif p in ("shorts", "youtube_shorts", "youtube"):
            out[p] = f"{hook} {fact}".strip()
        elif p == "tiktok":
            out[p] = f"{hook} {fact} Link in bio.".strip()
        elif p == "instagram":
            out[p] = f"{hook} {fact} Link in bio.".strip()
        else:
            out[p] = f"{hook} {fact} {url}".strip()
    return out


# ---------------------------------------------------------------------------
# Cutting
# ---------------------------------------------------------------------------

def _fmt_srt_time(s: float) -> str:
    h = int(s // 3600)
    m = int((s % 3600) // 60)
    sec = int(s % 60)
    ms = int(round((s - int(s)) * 1000))
    return f"{h:02d}:{m:02d}:{sec:02d},{ms:03d}"


def _make_srt(segments: List[Dict[str, Any]], start: float, end: float, path: str) -> int:
    in_range = []
    for seg in segments:
        ss, ee = seg["start"], seg["start"] + seg["duration"]
        if ee <= start + 0.01 or ss >= end - 0.01:
            continue
        in_range.append((max(ss, start), min(ee, end), seg["text"]))
    idx = 1
    lines: List[str] = []
    for i, (rs, re, text) in enumerate(in_range):
        if i + 1 < len(in_range):
            re = min(re, in_range[i + 1][0])
        re = min(re, end)
        rs = max(rs, start)
        rel_s = rs - start
        rel_e = re - start
        if rel_e - rel_s < 0.3:
            continue
        if rel_e - rel_s > 4.0:
            rel_e = rel_s + 4.0
        lines += [str(idx), f"{_fmt_srt_time(rel_s)} --> {_fmt_srt_time(rel_e)}", text.strip(), ""]
        idx += 1
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return idx - 1


def cut_clips(
    video_path: str,
    clips: List[Dict[str, Any]],
    out_dir: Optional[str] = None,
    segments: Optional[List[Dict[str, Any]]] = None,
    subs: bool = True,
) -> List[Dict[str, Any]]:
    """Cut clips from a source video with ffmpeg, burning subtitles."""
    ffmpeg = _ffmpeg()
    out_dir = out_dir or os.path.dirname(os.path.abspath(video_path)) or "."
    os.makedirs(out_dir, exist_ok=True)
    results: List[Dict[str, Any]] = []
    for c in clips:
        label = c.get("label") or f"clip_{int(c.get('start', 0))}"
        orient = c.get("orientation", "h")
        start = float(c["start"])
        end = float(c["end"])
        dur = end - start
        srt = os.path.join(out_dir, f"{label}.srt")
        out = os.path.join(out_dir, f"{label}_{orient}.mp4")
        n = 0
        if subs and segments:
            n = _make_srt(segments, start, end, srt)
        if orient == "v":
            base_vf = "crop=ih*9/16:ih,scale=1080:1920"
        else:
            base_vf = "scale=1920:1080"
        if subs and segments and os.path.exists(srt) and os.path.getsize(srt) > 0:
            if orient == "v":
                vf = (
                    f"subtitles={os.path.basename(srt)}:force_style='FontSize=20,"
                    "PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,Outline=2,Shadow=1',"
                    "crop=ih*9/16:ih,scale=1080:1920"
                )
            else:
                vf = (
                    f"subtitles={os.path.basename(srt)}:force_style='FontSize=22,"
                    "PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,Outline=2,Shadow=1',"
                    "scale=1920:1080"
                )
        else:
            vf = base_vf
        cmd = [
            ffmpeg, "-y", "-ss", f"{start:.3f}", "-i", video_path, "-t", f"{dur:.3f}",
            "-vf", vf, "-c:v", "libx264", "-preset", "fast", "-crf", "23",
            "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", out,
        ]
        r = subprocess.run(cmd, capture_output=True, text=True, cwd=out_dir)
        results.append({
            "label": label,
            "orientation": orient,
            "start": start,
            "end": end,
            "duration": round(dur, 2),
            "subs": n if subs else 0,
            "output": out if r.returncode == 0 else None,
            "error": r.stderr[-400:] if r.returncode != 0 else None,
        })
    return results


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

def run(
    url: str,
    count: int = 6,
    platforms: Optional[List[str]] = None,
    out_dir: Optional[str] = None,
    min_duration: float = 45,
    max_duration: float = 60,
) -> Dict[str, Any]:
    """Full pipeline: fetch transcript, score moments, write copy.

    Returns {video, transcript, clips, copy}. Clips carry the text needed for
    the agent to write final copy; the copy block is a deterministic first draft.
    """
    t0 = time.time()
    transcript = fetch_transcript(url, out_dir=out_dir)
    segments = transcript["segments"]
    clips = score_moments(segments, min_duration, max_duration, count)
    copy: Dict[str, Any] = {}
    for c in clips:
        c["copy"] = write_copy(c["text"], platforms, url=url, title=transcript.get("title", ""))
        copy[c["id"]] = c["copy"]
    return {
        "video": {
            "video_id": transcript["video_id"],
            "title": transcript["title"],
            "channel": transcript["channel"],
            "duration": transcript["duration"],
            "url": url,
        },
        "transcript": {
            "segment_count": transcript["segment_count"],
            "path": transcript["transcript_path"],
            "txt_path": transcript["transcript_txt"],
            "work_dir": transcript["work_dir"],
        },
        "clips": [
            {
                "id": c["id"],
                "start": c["start"],
                "end": c["end"],
                "duration": c["duration"],
                "scores": c["scores"],
                "total": c["total"],
                "text": c["text"][:600],
                "copy": c["copy"],
            }
            for c in clips
        ],
        "copy": copy,
        "elapsed_seconds": round(time.time() - t0, 1),
    }
