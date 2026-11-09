"""Content Engine core.

Fetch a YouTube transcript, score clip moments, write per-platform social
copy, and cut clips with ffmpeg. No API keys, no model tokens. yt-dlp and
ffmpeg are found on PATH with shutil.which. Every URL is restricted to
YouTube hosts and every output path is confined to the plugin work dir.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

# ---------------------------------------------------------------------------
# Tool discovery and safety boundaries
# ---------------------------------------------------------------------------


def _find_tool(name: str) -> str:
    """Locate a helper binary on PATH. No machine-specific absolute paths."""
    path = shutil.which(name)
    if not path:
        raise RuntimeError(
            f"{name} was not found. Install {name} and make sure it is on PATH."
        )
    return path


def _ytdlp() -> str:
    return _find_tool("yt-dlp")


def _ffmpeg() -> str:
    return _find_tool("ffmpeg")


_ALLOWED_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"}
_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def validate_video_url(url: str) -> str:
    """Return a normalized YouTube URL, or raise ValueError.

    Only https URLs on YouTube's own hosts are accepted. A leading '-' is
    rejected so the value can never be parsed as a yt-dlp option, and any
    userinfo or port is rejected so the authority cannot be redirected.
    """
    if not isinstance(url, str):
        raise ValueError("url must be a string")
    u = url.strip()
    if not u or u.startswith("-"):
        raise ValueError("invalid url: must not be empty or start with '-'")
    p = urlparse(u)
    if p.scheme != "https":
        raise ValueError("invalid url: https is required")
    host = (p.hostname or "").lower()
    if host not in _ALLOWED_HOSTS:
        raise ValueError(f"invalid url: {host!r} is not a YouTube host")
    if p.username or p.password or p.port:
        raise ValueError("invalid url: unexpected credentials or port")
    return u


def _hermes_home() -> Path:
    try:
        import hermes_constants
        return Path(hermes_constants.get_hermes_home())
    except Exception:
        pass
    env = os.environ.get("HERMES_HOME")
    if env:
        return Path(env)
    local = os.environ.get("LOCALAPPDATA")
    if local:
        return Path(local) / "hermes"
    return Path.home() / ".hermes"


def _plugin_work_dir() -> Path:
    base = (_hermes_home() / "plugin-data" / "content-engine").resolve()
    base.mkdir(parents=True, exist_ok=True)
    return base


def _resolve_out_dir(out_dir: Optional[str]) -> str:
    """Confine every output path to the plugin work dir.

    Absolute paths are allowed only when they already resolve inside the work
    dir. Traversal ('..') and outside-absolute paths are rejected, so a label
    or out_dir can never write .srt/.mp4 files elsewhere on the machine.
    """
    base = _plugin_work_dir()
    if not out_dir:
        return str(Path(tempfile.mkdtemp(prefix="content-engine-", dir=str(base))))
    requested = Path(str(out_dir))
    if not requested.is_absolute():
        requested = base / requested
    resolved = requested.resolve()
    try:
        resolved.relative_to(base)
    except ValueError:
        raise ValueError(
            "out_dir must resolve inside the plugin work dir: " + str(base)
        )
    resolved.mkdir(parents=True, exist_ok=True)
    return str(resolved)


def _safe_label(label: Any) -> str:
    """Limit a clip label to [A-Za-z0-9_-] so it cannot escape or inject."""
    s = str(label or "clip")
    if not _SAFE_NAME_RE.fullmatch(s):
        raise ValueError(
            "label must contain only letters, digits, '_' or '-' (got " + repr(s) + ")"
        )
    return s


def _escape_filter_path(path: str) -> str:
    """Escape a filename for an ffmpeg filter argument."""
    out = []
    for ch in path:
        if ch in ("\\", ":", "'", "[", "]", ",", ";"):
            out.append("\\")
        out.append(ch)
    return "".join(out)


# ---------------------------------------------------------------------------
# Transcript
# ---------------------------------------------------------------------------

_VIDEO_ID_RE = re.compile(r"(?:v=|youtu\.be/|/shorts/|/embed/)([A-Za-z0-9_-]{11})")


def video_id(url: str) -> str:
    m = _VIDEO_ID_RE.search(url)
    return m.group(1) if m else url.strip()


def _strip_vtt(s: str) -> str:
    s = re.sub(r"<[^>]+>", "", s)
    return (s.replace("&amp;", "&").replace("&lt;", "<")
             .replace("&gt;", ">").replace("&nbsp;", " ")).strip()


def _parse_vtt(path: str) -> List[Dict[str, Any]]:
    """Parse YouTube VTT, handling both plain captions and auto-caption
    rolling cues (which repeat the previous line plus new karaoke-tagged words).
    """
    with open(path, encoding="utf-8") as f:
        content = f.read()
    segments: List[Dict[str, Any]] = []
    prev_text = ""
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
        dur = end - start
        text_lines = lines[timing_idx + 1:]
        raw = "\n".join(text_lines)
        # Auto-caption rolling cues carry word-level karaoke tags. The last
        # line holds the NEW spoken words; earlier lines are the previous
        # rolling line repeated. Plain captions join all lines.
        if re.search(r"<c>|<\d\d:\d\d:", raw):
            text = _strip_vtt(text_lines[-1]) if text_lines else ""
        else:
            text = " ".join(_strip_vtt(l) for l in text_lines if _strip_vtt(l))
        text = text.strip()
        # Skip 10ms "clear" cues and exact consecutive repeats.
        if not text or dur < 0.12 or text == prev_text:
            continue
        segments.append({"start": round(start, 2), "duration": round(dur, 2), "text": text})
        prev_text = text
    return segments


def fetch_transcript(url: str, out_dir: Optional[str] = None) -> Dict[str, Any]:
    """Download the transcript and metadata for a YouTube video.

    Returns {video_id, title, channel, duration, segment_count, transcript_path,
    transcript_txt, segments}.
    """
    yt = _ytdlp()
    url = validate_video_url(url)
    out_dir = _resolve_out_dir(out_dir)
    os.makedirs(out_dir, exist_ok=True)
    vid = video_id(url)

    meta = subprocess.run(
        [yt, "--skip-download", "--print", "%(title)s", "--print", "%(channel)s",
         "--print", "%(duration_string)s", "--", url],
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
         "--sub-format", "vtt", "-o", os.path.join(out_dir, "transcript"), "--", url],
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

def _excerpt(text: str, limit: int) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text.rstrip(".,;:")
    return text[:limit].rsplit(" ", 1)[0].rstrip(".,;:")


def _first_sentence(text: str) -> str:
    """First complete sentence with at least 4 words, capped."""
    for m in re.finditer(r"[^.!?]+[.!?]", text):
        s = m.group(0).strip()
        if len(s.split()) >= 4:
            return _excerpt(s, 120)
    return _excerpt(text, 120)


def _key_fact(text: str, hook: str) -> str:
    """A sentence with a number or name that is meaningfully different from the hook."""
    best = ""
    for s in re.split(r"(?<=[.!?])\s+", text):
        s = " ".join(s.split())
        if not s or s == hook:
            continue
        if (_NUMBER_RE.search(s) or _NAME_RE.search(s)) and len(s) <= 160:
            if len(s) > len(best):
                best = s
    return _excerpt(best, 120) if best else ""


def write_copy(
    clip_text: str,
    platforms: Optional[List[str]] = None,
    url: str = "",
    title: str = "",
) -> Dict[str, str]:
    """Deterministic first-draft social copy per platform."""
    platforms = platforms or ["x", "facebook", "shorts", "tiktok"]
    hook = _first_sentence(clip_text)
    fact = _key_fact(clip_text, hook)
    # Auto-caption text has no sentence breaks, so hook and fact can overlap.
    # Never ship the same words twice.
    if fact and (fact in hook or hook in fact or fact[:30] in hook):
        fact = ""
    body = f"{hook} {fact}".strip() if fact else hook
    out: Dict[str, str] = {}
    for p in platforms:
        p = p.lower()
        if p == "x":
            out[p] = f"{body} {url}".strip()
        elif p == "facebook":
            out[p] = f"{body}\n\n{url}".strip()
        elif p in ("shorts", "youtube_shorts", "youtube"):
            out[p] = body
        elif p == "tiktok":
            out[p] = f"{body} Link in bio."
        elif p == "instagram":
            out[p] = f"{body} Link in bio."
        else:
            out[p] = f"{body} {url}".strip()
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
    out_dir = _resolve_out_dir(out_dir)
    os.makedirs(out_dir, exist_ok=True)
    results: List[Dict[str, Any]] = []
    for c in clips:
        label = _safe_label(c.get("label") or f"clip_{int(c.get('start', 0))}")
        orient = c.get("orientation", "h")
        if orient not in ("h", "v"):
            raise ValueError("orientation must be 'h' or 'v'")
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
        sub_arg = _escape_filter_path(os.path.basename(srt))
        if subs and segments and os.path.exists(srt) and os.path.getsize(srt) > 0:
            if orient == "v":
                vf = (
                    f"subtitles={sub_arg}:force_style='FontSize=20,"
                    "PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,Outline=2,Shadow=1',"
                    "crop=ih*9/16:ih,scale=1080:1920"
                )
            else:
                vf = (
                    f"subtitles={sub_arg}:force_style='FontSize=22,"
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
