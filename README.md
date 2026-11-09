# Content Engine for Hermes Agent

One long video in, a week of content out. Paste a YouTube URL, get scored 45 to 60 second clips and per-platform social copy. Live in 90 seconds, no editing software.

## What it does

Five tools in the `content-engine` toolset:

- `ce_fetch_transcript` — fetch the timestamped transcript and metadata for a YouTube video.
- `ce_score_moments` — score transcript segments and return the top non-overlapping 45 to 60 second clip candidates.
- `ce_write_copy` — write a first draft of per-platform social copy from a clip transcript.
- `ce_cut_clips` — cut clips from a downloaded video with ffmpeg, burning in subtitles.
- `ce_run` — the whole pipeline in one call. Fetch, score, and draft copy.

The scoring uses four criteria, each 1 to 5, for a max of 20 plus a length bonus:

- Hook — does the first sentence open with a specific case, number, or vivid image?
- Completeness — is it a self-contained mini-story with no dangling references?
- Standalone value — does a viewer who never saw the full video still walk away with something?
- Visual payoff — does the narration line up with an illustration, photo, or footage?

Candidates are segment-aligned windows, scored, sorted, and deduped so the top picks do not overlap. The copy tool writes a deterministic first draft for X, Facebook, YouTube Shorts, TikTok, and Instagram. The agent can refine the draft in chat.

## Install

```bash
hermes plugins install PXMichael/content-engine
hermes plugins enable content-engine
```

Or from the catalog once listed:

```bash
hermes plugins install content-engine
```

## Setup

Content Engine uses two external tools, both free:

- **yt-dlp** downloads the transcript.
- **ffmpeg** cuts the clips.

Both are found on PATH with `shutil.which`. No absolute binary paths are baked in. Install them and make sure they are on PATH.

No API keys. No model tokens. No account.

## Demo

Paste a long YouTube URL into Hermes and run `ce_run`:

```
Run ce_run on https://www.youtube.com/watch?v=aircAruvnKk and give me the top 3 clips with copy.
```

Hermes fetches the transcript, scores the moments, and returns the top clips with a first draft of the copy. The whole run takes about 8 seconds for an 18 minute video.

To cut the clips, download the source with yt-dlp and pass the path:

```
yt-dlp -f bestvideo[ext=mp4]+bestaudio[ext=m4a]/best -o source.mp4 <url>
```

Then call `ce_cut_clips` with the source path, the clip list, and the transcript segments.

## How it works

1. **Fetch** — yt-dlp downloads the English captions (manual or auto-generated) and writes a VTT file. The plugin parses the VTT into timestamped segments.
2. **Score** — the plugin builds segment-aligned candidate windows, scores each on hook, completeness, standalone value, and visual payoff, then picks the top non-overlapping windows.
3. **Copy** — the plugin extracts the hook and the key fact from each clip and writes a first draft per platform. X and Facebook get the link. Shorts and TikTok get "Link in bio."
4. **Cut** — ffmpeg cuts each window, burns the subtitles, and writes horizontal and vertical mp4s.

## Security notes

- The plugin reads no secrets and no `.env` files. It needs no credentials.
- It calls yt-dlp and ffmpeg through list-form subprocess calls with no shell. There is no approval step. The agent or user decides when to call a tool; the tool itself does not ask again.
- Every URL is restricted to `https://` on `youtube.com`, `www.youtube.com`, `m.youtube.com`, or `youtu.be` before yt-dlp sees it. Values starting with `-` are rejected so they cannot become yt-dlp options, and `--` is passed before the URL in both calls.
- Every output path is confined to `get_hermes_home()/plugin-data/content-engine/`. An absolute or `../` `out_dir` is rejected, and clip labels are limited to `[A-Za-z0-9_-]`.
- It makes network calls only to YouTube, through yt-dlp.
- The copy tool writes a first draft. It does not post anywhere. You review and post.

## Development

Validate like catalog CI does:

```bash
hermes plugins validate ./content-engine --install-deps
```

Run tests:

```bash
python -m pytest tests/
```

## License

MIT
