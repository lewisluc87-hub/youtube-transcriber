# yt-transcriber

[![CI](https://github.com/lewisluc87-hub/youtube-transcriber/actions/workflows/ci.yml/badge.svg)](https://github.com/lewisluc87-hub/youtube-transcriber/actions/workflows/ci.yml)

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

# MCP server (see "MCP server" below)
pip install mcp==2.2.0
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

## MCP server

`mcp_server.py` exposes the transcript functionality to any MCP client (Claude Desktop, etc.). It calls the real functions in `transcribe.py` (`extract_video_id`, `fetch_metadata`, `fetch_captions`, `render_txt`) rather than reimplementing them, so a fix to caption sourcing applies to both the CLI and the server.

**Tools:**
- `get_video_info(url)` — title, channel, duration, and whether captions exist. Does not return transcript text. (It does fetch the captions internally to check, which is cheap text-only work.)
- `get_transcript(url, offset=0, max_chars=20000)` — the timestamped transcript, paginated; the response tells you the `next_offset` to request if there is more. An hour of speech is roughly 60,000 characters.
- `list_archived_transcripts()` — videos the CLI has already saved under `output/`.
- `read_archived_transcript(video_id)` — reads a saved transcript back. Offline; never contacts YouTube.

**Deliberately narrower than the CLI:**
- **No AI summaries.** `generate_summary()` spends your API credits on every call, and the MCP client can summarize the returned text itself. The server needs no API key and never spends money on its own.
- **No Whisper fallback.** It is a multi-minute CPU job with a first-run model download, a poor fit for a synchronous tool call. A video with no captions returns a clear message pointing at the CLI (`python transcribe.py <url>`).
- **Never writes to disk.** `get_transcript` returns text directly instead of calling `write_outputs()`. Only the archive tools touch the filesystem, and only to read what the CLI already wrote.

**Inputs are checked before anything is fetched.** URLs must be a bare 11-character video ID or an `http(s)` URL on `youtube.com` (including the `www.` and `m.` forms) or `youtu.be`. `transcribe.py` already only ever uses the extracted ID (it rebuilds the watch URL itself), so this is defense-in-depth on an already-safe design. `read_archived_transcript` accepts only an 11-character ID, which rules out path traversal. Transcript text is returned under an "untrusted third-party text" header: captions are written by whoever uploaded the video, and a client should treat them as data, not instructions.

**Install and run** (`mcp` is a separate, optional install, like `faster-whisper` and `anthropic` above, and is listed in `requirements-optional.txt`):
```bash
pip install mcp==2.2.0
python mcp_server.py        # stdio transport, what Claude Desktop expects
```

**Claude Desktop config** (`claude_desktop_config.json`):
```json
{
  "mcpServers": {
    "youtube-transcriber": {
      "command": "python",
      "args": ["/absolute/path/to/youtube-transcriber/mcp_server.py"]
    }
  }
}
```

### How it was verified, and what was not

- `test_mcp_server.py` drives the real server as a subprocess over real stdio with the official `mcp` client (not a mock of the protocol). `transcribe.py` imports `yt_dlp` and `youtube_transcript_api` lazily, so the subprocess's `PYTHONPATH` is pointed at `mcp_test_shims/` (fake versions of those two libraries). The real server therefore runs the real `transcribe.py` logic against fake network calls, without patching anything inside `transcribe.py` or `mcp_server.py`.
- **stdout protection is tested, not assumed.** MCP over stdio uses stdout for the protocol stream, and `transcribe.py` `print()`s progress messages. `mcp_server.py` redirects stdout around calls into it, and a test makes the fake library print mid-call, then confirms the session survives and a second call still works.
- Also covered: tool listing, captions / no-captions / unavailable-video paths, pagination offsets (no overlap between pages), rejection of non-YouTube URLs, and the archive tools including a path-traversal attempt.
- **Not verified in the environment this was built in:** the live YouTube path. That environment had no route to youtube.com, so nothing here has been run against a real video. `smoke_test_mcp.sh` is the live check (it reuses `smoke_test.sh`'s video URLs); run it on a machine with real YouTube access before relying on this. It was syntax-checked but not run.

## Testing

```bash
pip install pytest pytest-asyncio mcp==2.2.0
pytest                                  # CLI unit tests + MCP server tests, fully offline
bash smoke_test.sh                      # live end-to-end test of the CLI against real videos
bash smoke_test_mcp.sh                  # live end-to-end test of the MCP server (needs real YouTube access)
```

## License

MIT
