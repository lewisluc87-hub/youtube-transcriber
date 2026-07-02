#!/usr/bin/env python3
"""yt-transcriber: fetch a transcript (and optional AI summary) for a YouTube video.

Sourcing strategy (captions-first):
  1. Manually created captions via youtube-transcript-api
  2. Auto-generated captions via youtube-transcript-api
  3. Fallback: download audio with yt-dlp and transcribe locally with faster-whisper

Summary (optional): generated only if ANTHROPIC_API_KEY or OPENAI_API_KEY is set.

Usage:
  python transcribe.py <youtube_url> [--srt] [--no-summary] [--force] [--output-dir DIR]
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

MAX_DURATION_SECONDS = 60 * 60  # 1 hour guard before expensive Whisper jobs
DEFAULT_OUTPUT_DIR = "output"

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class Segment:
    start: float  # seconds
    duration: float  # seconds
    text: str


@dataclass
class TranscriptResult:
    video_id: str
    title: str
    channel: str
    duration: int  # seconds (0 if unknown)
    url: str
    source: str  # "manual-captions" | "auto-captions" | "whisper"
    segments: list[Segment] = field(default_factory=list)

    @property
    def full_text(self) -> str:
        return " ".join(s.text.strip() for s in self.segments if s.text.strip())


class TranscriberError(Exception):
    """User-facing error. Message is printed without a traceback."""


# ---------------------------------------------------------------------------
# URL handling
# ---------------------------------------------------------------------------

_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")

_URL_PATTERNS = [
    re.compile(r"(?:v=|/videos/|embed/|youtu\.be/|/v/|/e/|shorts/|live/)([A-Za-z0-9_-]{11})"),
]


def extract_video_id(url: str) -> str:
    """Extract the 11-char video ID from common YouTube URL shapes."""
    url = url.strip()
    if _VIDEO_ID_RE.match(url):
        return url  # bare video ID
    if not re.match(r"^https?://", url):
        raise TranscriberError(f"Not a valid URL or video ID: {url!r}")
    if not re.search(r"(youtube\.com|youtu\.be)", url):
        raise TranscriberError(f"Not a YouTube URL: {url!r}")
    for pat in _URL_PATTERNS:
        m = pat.search(url)
        if m:
            return m.group(1)
    raise TranscriberError(f"Could not find a video ID in URL: {url!r}")


def slugify(text: str, max_len: int = 60) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").lower()
    return slug[:max_len].rstrip("-") or "untitled"


# ---------------------------------------------------------------------------
# Metadata (yt-dlp, no download)
# ---------------------------------------------------------------------------


def fetch_metadata(video_id: str) -> dict:
    import yt_dlp

    url = f"https://www.youtube.com/watch?v={video_id}"
    opts = {"quiet": True, "no_warnings": True, "skip_download": True}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as e:
        msg = str(e)
        if "Private video" in msg:
            raise TranscriberError("This video is private.") from e
        if "unavailable" in msg.lower():
            raise TranscriberError("This video is unavailable (deleted or region-locked).") from e
        if re.search(r"age[ -]restrict|confirm your age|sign in to confirm", msg, re.IGNORECASE):
            raise TranscriberError("This video is age-restricted and cannot be accessed.") from e
        raise TranscriberError(f"Could not fetch video metadata: {msg}") from e
    return {
        "title": info.get("title") or video_id,
        "channel": info.get("channel") or info.get("uploader") or "unknown",
        "duration": int(info.get("duration") or 0),
    }


# ---------------------------------------------------------------------------
# Caption fetching (youtube-transcript-api v1.x)
# ---------------------------------------------------------------------------


def fetch_captions(video_id: str) -> tuple[list[Segment], str] | None:
    """Return (segments, source) using manual captions first, then auto.

    Returns None if the video simply has no captions (caller falls back to
    Whisper). Raises TranscriberError for hard failures (video unavailable).
    """
    from youtube_transcript_api import YouTubeTranscriptApi
    from youtube_transcript_api._errors import (
        CouldNotRetrieveTranscript,
        NoTranscriptFound,
        TranscriptsDisabled,
        VideoUnavailable,
    )

    api = YouTubeTranscriptApi()
    try:
        transcript_list = api.list(video_id)
    except (TranscriptsDisabled, NoTranscriptFound):
        return None
    except VideoUnavailable as e:
        raise TranscriberError("This video is unavailable (private, deleted, or region-locked).") from e
    except CouldNotRetrieveTranscript:
        return None

    for finder, source in (
        (transcript_list.find_manually_created_transcript, "manual-captions"),
        (transcript_list.find_generated_transcript, "auto-captions"),
    ):
        try:
            transcript = finder(["en", "en-US", "en-GB"])
        except NoTranscriptFound:
            # Fall back to whatever language exists
            try:
                langs = [t.language_code for t in transcript_list]
                transcript = finder(langs) if langs else None
            except NoTranscriptFound:
                transcript = None
        if transcript is None:
            continue
        try:
            fetched = transcript.fetch()
        except CouldNotRetrieveTranscript:
            continue
        segments = [Segment(start=s.start, duration=s.duration, text=s.text) for s in fetched]
        if segments:
            return segments, source
    return None


# ---------------------------------------------------------------------------
# Whisper fallback (lazy import — faster-whisper is an optional dependency)
# ---------------------------------------------------------------------------


def transcribe_with_whisper(video_id: str, model_size: str = "base") -> list[Segment]:
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        raise TranscriberError(
            "No captions found for this video, and faster-whisper is not installed.\n"
            "Install the audio-transcription fallback with:\n"
            "    pip install faster-whisper"
        )
    import yt_dlp

    tmpdir = tempfile.mkdtemp(prefix="ytt-audio-")
    try:
        audio_path = os.path.join(tmpdir, f"{video_id}.m4a")
        opts = {
            "quiet": True,
            "no_warnings": True,
            "format": "bestaudio[ext=m4a]/bestaudio/best",
            "outtmpl": audio_path,
        }
        url = f"https://www.youtube.com/watch?v={video_id}"
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([url])
        except yt_dlp.utils.DownloadError as e:
            raise TranscriberError(f"Audio download failed: {e}") from e

        print(f"Transcribing audio with faster-whisper ({model_size})... this may take a few minutes.")
        model = WhisperModel(model_size, device="cpu", compute_type="int8")
        raw_segments, _info = model.transcribe(audio_path, vad_filter=True)
        segments = [
            Segment(start=s.start, duration=max(s.end - s.start, 0.0), text=s.text)
            for s in raw_segments
        ]
        if not segments:
            raise TranscriberError("Whisper produced no transcript (is the video silent?).")
        return segments
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Summarization (optional)
# ---------------------------------------------------------------------------

SUMMARY_PROMPT = (
    "Summarize the following YouTube video transcript. Provide:\n"
    "1. A one-paragraph overview\n"
    "2. Key points as a short bulleted list\n"
    "3. Any notable conclusions or takeaways\n\n"
    "Title: {title}\nChannel: {channel}\n\nTranscript:\n{transcript}"
)

# Keep prompts within typical context limits; truncate very long transcripts.
MAX_SUMMARY_INPUT_CHARS = 150_000


def generate_summary(result: TranscriptResult) -> str | None:
    """Return a summary string, or None if no API key configured."""
    text = result.full_text[:MAX_SUMMARY_INPUT_CHARS]
    prompt = SUMMARY_PROMPT.format(title=result.title, channel=result.channel, transcript=text)

    if os.environ.get("ANTHROPIC_API_KEY"):
        try:
            import anthropic

            client = anthropic.Anthropic()
            resp = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=1024,
                messages=[{"role": "user", "content": prompt}],
            )
            return "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        except ImportError:
            print("ANTHROPIC_API_KEY is set but the 'anthropic' package is not installed "
                  "(pip install anthropic). Skipping summary.", file=sys.stderr)
            return None
        except Exception as e:  # noqa: BLE001 - summary is best-effort
            print(f"Summary generation failed ({e}). Transcript was still saved.", file=sys.stderr)
            return None

    if os.environ.get("OPENAI_API_KEY"):
        try:
            from openai import OpenAI

            client = OpenAI()
            resp = client.chat.completions.create(
                model="gpt-4o-mini",
                max_tokens=1024,
                messages=[{"role": "user", "content": prompt}],
            )
            return resp.choices[0].message.content
        except ImportError:
            print("OPENAI_API_KEY is set but the 'openai' package is not installed "
                  "(pip install openai). Skipping summary.", file=sys.stderr)
            return None
        except Exception as e:  # noqa: BLE001
            print(f"Summary generation failed ({e}). Transcript was still saved.", file=sys.stderr)
            return None

    return None  # no key configured — silently skip per spec


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------


def format_timestamp(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def format_srt_timestamp(seconds: float) -> str:
    ms = int(round((seconds - int(seconds)) * 1000))
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def render_txt(result: TranscriptResult) -> str:
    lines = [f"[{format_timestamp(seg.start)}] {seg.text.strip()}" for seg in result.segments]
    return "\n".join(lines) + "\n"


def render_srt(result: TranscriptResult) -> str:
    blocks = []
    for i, seg in enumerate(result.segments, start=1):
        start = format_srt_timestamp(seg.start)
        end = format_srt_timestamp(seg.start + seg.duration)
        blocks.append(f"{i}\n{start} --> {end}\n{seg.text.strip()}\n")
    return "\n".join(blocks)


def render_summary_md(result: TranscriptResult, summary: str) -> str:
    return (
        f"# {result.title}\n\n"
        f"- **Channel:** {result.channel}\n"
        f"- **URL:** {result.url}\n"
        f"- **Duration:** {format_timestamp(result.duration)}\n"
        f"- **Transcript source:** {result.source}\n\n"
        f"## Summary\n\n{summary}\n"
    )


def write_outputs(result: TranscriptResult, summary: str | None, output_dir: str, srt: bool) -> Path:
    folder = Path(output_dir) / f"{result.video_id}-{slugify(result.title)}"
    folder.mkdir(parents=True, exist_ok=True)
    if srt:
        (folder / "transcript.srt").write_text(render_srt(result), encoding="utf-8")
    else:
        (folder / "transcript.txt").write_text(render_txt(result), encoding="utf-8")
    if summary:
        (folder / "summary.md").write_text(render_summary_md(result, summary), encoding="utf-8")
    return folder


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------


def run(url: str, *, srt: bool = False, no_summary: bool = False, force: bool = False,
        output_dir: str = DEFAULT_OUTPUT_DIR, whisper_model: str = "base") -> Path:
    video_id = extract_video_id(url)
    canonical_url = f"https://www.youtube.com/watch?v={video_id}"

    print(f"Fetching metadata for {video_id}...")
    meta = fetch_metadata(video_id)
    duration = meta["duration"]
    print(f"  {meta['title']}  ({meta['channel']}, {format_timestamp(duration)})")

    print("Looking for captions...")
    caption_result = fetch_captions(video_id)

    if caption_result is not None:
        segments, source = caption_result
        print(f"  Found {source} ({len(segments)} segments).")
    else:
        print("  No captions available — falling back to local Whisper transcription.")
        if duration > MAX_DURATION_SECONDS and not force:
            raise TranscriberError(
                f"Video is {format_timestamp(duration)} long (over the 1-hour guard) and has no "
                "captions, so transcription would use Whisper and take a while.\n"
                "Re-run with --force to proceed anyway."
            )
        segments = transcribe_with_whisper(video_id, model_size=whisper_model)
        source = "whisper"

    result = TranscriptResult(
        video_id=video_id,
        title=meta["title"],
        channel=meta["channel"],
        duration=duration,
        url=canonical_url,
        source=source,
        segments=segments,
    )

    summary = None
    if not no_summary:
        summary = generate_summary(result)
        if summary is None and not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("OPENAI_API_KEY")):
            print("No ANTHROPIC_API_KEY/OPENAI_API_KEY set — skipping summary.")

    folder = write_outputs(result, summary, output_dir, srt)
    print(f"\nDone. Output written to: {folder}/")
    if summary:
        print("\n--- Summary ---\n")
        print(summary)
    return folder


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Transcribe (and optionally summarize) a YouTube video.")
    parser.add_argument("url", help="YouTube video URL or bare 11-character video ID")
    parser.add_argument("--srt", action="store_true", help="write transcript.srt instead of transcript.txt")
    parser.add_argument("--no-summary", action="store_true", help="skip AI summary even if an API key is set")
    parser.add_argument("--force", action="store_true", help="bypass the 1-hour duration guard for Whisper jobs")
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR, help="base output directory (default: output/)")
    parser.add_argument("--whisper-model", default="base",
                        help="faster-whisper model size for the no-captions fallback (default: base)")
    args = parser.parse_args(argv)

    try:
        run(args.url, srt=args.srt, no_summary=args.no_summary, force=args.force,
            output_dir=args.output_dir, whisper_model=args.whisper_model)
    except TranscriberError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
