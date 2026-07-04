# yt-transcriber

Paste a YouTube link, get a transcript and AI summary — no watching required. Captions-first, with a local Whisper fallback for videos that have none. Every run is saved to a per-video folder, so you build a local archive as you go.

## Install

```bash
git clone <this-repo>
cd yt-transcriber
pip install -r requirements.txt
```

Optional extras:

```bash
# Whisper fallback for videos with no captions
pip install faster-whisper

# AI summaries (install the one matching your API key)
pip install anthropic   # uses ANTHROPIC_API_KEY
pip install openai      # uses OPENAI_API_KEY
```

## Usage

```bash
python transcribe.py "https://www.youtube.com/watch?v=VIDEO_ID"
```

Output goes to `output/<video-id>-<title-slug>/`:

- `transcript.txt` — timestamped transcript (`[00:01:23] ...`)
- `summary.md` — AI summary (only if an API key is configured)

### Flags

| Flag | Effect |
|---|---|
| `--srt` | Write `transcript.srt` (SubRip format) instead of `.txt` |
| `--no-summary` | Skip the AI summary even if an API key is set |
| `--force` | Bypass the 1-hour guard for Whisper jobs on long captionless videos |
| `--output-dir DIR` | Change the base output directory (default `output/`) |
| `--whisper-model SIZE` | faster-whisper model for the fallback (default `base`; try `small` or `medium` for better accuracy) |
| `--force-whisper` | Debug: skip caption lookup and force local Whisper transcription, even if captions exist (useful for testing the fallback path on a video that already has captions) |

### AI summaries (optional)

Set one of these — either as a real environment variable, or in a `.env` file in the project root (copy `.env.example` to `.env` and fill it in). A real environment variable always takes precedence if both are set. The tool works fine with neither, it just skips the summary:

```bash
export ANTHROPIC_API_KEY=sk-ant-...   # preferred if both are set
# or
export OPENAI_API_KEY=sk-...
```

Or via `.env`:
```
ANTHROPIC_API_KEY=sk-ant-...
```

Never commit keys. `.gitignore` already excludes `.env` — only `.env.example` (with no real key in it) is tracked.

## Scope

Single videos only. No playlists, channels, live streams, or web UI (yet). Accuracy is caption-grade — great for digesting content, not for legal transcription.

## Testing

```bash
pip install pytest
python -m pytest test_transcribe.py    # unit tests, fully offline (YouTube mocked)
bash smoke_test.sh                      # live end-to-end test against real videos
```

## License

MIT