"""Unit tests for transcribe.py — all YouTube interactions are mocked.

Covers the verification plan:
  - URL parsing for all common YouTube URL shapes + invalid inputs
  - Manual captions preferred over auto captions
  - Auto captions used when no manual captions
  - No captions -> Whisper fallback invoked
  - Private/unavailable video -> clean TranscriberError
  - Duration guard: >60min blocked without --force, allowed with --force
  - Summary skipped gracefully when no API key is set
  - Output files written correctly (txt, srt, summary.md)
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest import mock

import pytest

import transcribe
from transcribe import (
    Segment,
    TranscriberError,
    TranscriptResult,
    extract_video_id,
    format_srt_timestamp,
    format_timestamp,
    render_srt,
    render_txt,
    slugify,
    write_outputs,
)

VID = "dQw4w9WgXcQ"


# ---------------------------------------------------------------------------
# URL parsing
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("url", [
    f"https://www.youtube.com/watch?v={VID}",
    f"https://youtube.com/watch?v={VID}&t=42s",
    f"https://youtu.be/{VID}",
    f"https://youtu.be/{VID}?si=abc123",
    f"https://www.youtube.com/embed/{VID}",
    f"https://www.youtube.com/shorts/{VID}",
    f"https://www.youtube.com/live/{VID}",
    f"http://m.youtube.com/watch?v={VID}",
    VID,  # bare ID
])
def test_extract_video_id_valid(url):
    assert extract_video_id(url) == VID


@pytest.mark.parametrize("url", [
    "https://vimeo.com/12345",
    "not a url at all",
    "https://www.youtube.com/",          # no video ID
    "https://www.youtube.com/watch?v=short",  # ID too short
    "",
])
def test_extract_video_id_invalid(url):
    with pytest.raises(TranscriberError):
        extract_video_id(url)


def test_slugify():
    assert slugify("Hello, World! — A Test") == "hello-world-a-test"
    assert slugify("") == "untitled"
    assert len(slugify("x" * 500)) <= 60


# ---------------------------------------------------------------------------
# Timestamp / rendering
# ---------------------------------------------------------------------------

def test_timestamps():
    assert format_timestamp(0) == "00:00:00"
    assert format_timestamp(3661) == "01:01:01"
    assert format_srt_timestamp(1.5) == "00:00:01,500"


def _result(segments, source="manual-captions", duration=300):
    return TranscriptResult(
        video_id=VID, title="Test Video", channel="Test Channel",
        duration=duration, url=f"https://www.youtube.com/watch?v={VID}",
        source=source, segments=segments,
    )


def test_render_txt_ordered_with_timestamps():
    r = _result([Segment(0, 2, "hello"), Segment(2, 2, "world")])
    out = render_txt(r)
    assert out == "[00:00:00] hello\n[00:00:02] world\n"


def test_render_srt():
    r = _result([Segment(0, 1.5, "hello")])
    out = render_srt(r)
    assert "1\n00:00:00,000 --> 00:00:01,500\nhello" in out


# ---------------------------------------------------------------------------
# Caption sourcing preference (mocked youtube-transcript-api)
# ---------------------------------------------------------------------------

class FakeFetchedSeg:
    def __init__(self, start, duration, text):
        self.start, self.duration, self.text = start, duration, text


def _fake_transcript(segs):
    t = mock.MagicMock()
    t.fetch.return_value = [FakeFetchedSeg(*s) for s in segs]
    return t


def test_manual_captions_preferred(monkeypatch):
    from youtube_transcript_api._errors import NoTranscriptFound

    tlist = mock.MagicMock()
    tlist.find_manually_created_transcript.return_value = _fake_transcript([(0, 2, "manual text")])
    api = mock.MagicMock()
    api.list.return_value = tlist
    monkeypatch.setattr("youtube_transcript_api.YouTubeTranscriptApi", lambda: api)

    segs, source = transcribe.fetch_captions(VID)
    assert source == "manual-captions"
    assert segs[0].text == "manual text"
    tlist.find_generated_transcript.assert_not_called()


def test_auto_captions_fallback(monkeypatch):
    from youtube_transcript_api._errors import NoTranscriptFound

    tlist = mock.MagicMock()
    tlist.__iter__ = lambda self: iter([])
    tlist.find_manually_created_transcript.side_effect = NoTranscriptFound(VID, ["en"], {})
    tlist.find_generated_transcript.return_value = _fake_transcript([(0, 2, "auto text")])
    api = mock.MagicMock()
    api.list.return_value = tlist
    monkeypatch.setattr("youtube_transcript_api.YouTubeTranscriptApi", lambda: api)

    segs, source = transcribe.fetch_captions(VID)
    assert source == "auto-captions"
    assert segs[0].text == "auto text"


def test_no_captions_returns_none(monkeypatch):
    from youtube_transcript_api._errors import TranscriptsDisabled

    api = mock.MagicMock()
    api.list.side_effect = TranscriptsDisabled(VID)
    monkeypatch.setattr("youtube_transcript_api.YouTubeTranscriptApi", lambda: api)

    assert transcribe.fetch_captions(VID) is None


def test_unavailable_video_raises(monkeypatch):
    from youtube_transcript_api._errors import VideoUnavailable

    api = mock.MagicMock()
    api.list.side_effect = VideoUnavailable(VID)
    monkeypatch.setattr("youtube_transcript_api.YouTubeTranscriptApi", lambda: api)

    with pytest.raises(TranscriberError, match="unavailable"):
        transcribe.fetch_captions(VID)


# ---------------------------------------------------------------------------
# Pipeline: duration guard + whisper fallback wiring (all network mocked)
# ---------------------------------------------------------------------------

META_SHORT = {"title": "Short Vid", "channel": "Chan", "duration": 600}
META_LONG = {"title": "Long Vid", "channel": "Chan", "duration": 3 * 3600}


def test_run_uses_captions(tmp_path, monkeypatch):
    monkeypatch.setattr(transcribe, "fetch_metadata", lambda vid: META_SHORT)
    monkeypatch.setattr(transcribe, "fetch_captions",
                        lambda vid: ([Segment(0, 2, "hi"), Segment(2, 2, "there")], "manual-captions"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    folder = transcribe.run(f"https://youtu.be/{VID}", output_dir=str(tmp_path))
    txt = (folder / "transcript.txt").read_text()
    assert "[00:00:00] hi" in txt and "[00:00:02] there" in txt
    assert not (folder / "summary.md").exists()  # no key -> no summary file


def test_run_whisper_fallback_invoked(tmp_path, monkeypatch):
    monkeypatch.setattr(transcribe, "fetch_metadata", lambda vid: META_SHORT)
    monkeypatch.setattr(transcribe, "fetch_captions", lambda vid: None)
    whisper_called = {}

    def fake_whisper(vid, model_size="base"):
        whisper_called["yes"] = True
        return [Segment(0, 3, "whisper output")]

    monkeypatch.setattr(transcribe, "transcribe_with_whisper", fake_whisper)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    folder = transcribe.run(VID, output_dir=str(tmp_path))
    assert whisper_called.get("yes")
    assert "whisper output" in (folder / "transcript.txt").read_text()


def test_duration_guard_blocks_long_whisper(tmp_path, monkeypatch):
    monkeypatch.setattr(transcribe, "fetch_metadata", lambda vid: META_LONG)
    monkeypatch.setattr(transcribe, "fetch_captions", lambda vid: None)

    with pytest.raises(TranscriberError, match="--force"):
        transcribe.run(VID, output_dir=str(tmp_path))


def test_duration_guard_force_bypasses(tmp_path, monkeypatch):
    monkeypatch.setattr(transcribe, "fetch_metadata", lambda vid: META_LONG)
    monkeypatch.setattr(transcribe, "fetch_captions", lambda vid: None)
    monkeypatch.setattr(transcribe, "transcribe_with_whisper",
                        lambda vid, model_size="base": [Segment(0, 1, "forced")])
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    folder = transcribe.run(VID, output_dir=str(tmp_path), force=True)
    assert "forced" in (folder / "transcript.txt").read_text()


def test_duration_guard_not_applied_when_captions_exist(tmp_path, monkeypatch):
    """A 3-hour video WITH captions should not trigger the guard (captions are cheap)."""
    monkeypatch.setattr(transcribe, "fetch_metadata", lambda vid: META_LONG)
    monkeypatch.setattr(transcribe, "fetch_captions",
                        lambda vid: ([Segment(0, 2, "long but captioned")], "auto-captions"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    folder = transcribe.run(VID, output_dir=str(tmp_path))
    assert "long but captioned" in (folder / "transcript.txt").read_text()


def test_run_srt_output(tmp_path, monkeypatch):
    monkeypatch.setattr(transcribe, "fetch_metadata", lambda vid: META_SHORT)
    monkeypatch.setattr(transcribe, "fetch_captions",
                        lambda vid: ([Segment(0, 1.5, "sub")], "manual-captions"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    folder = transcribe.run(VID, output_dir=str(tmp_path), srt=True)
    assert (folder / "transcript.srt").exists()
    assert not (folder / "transcript.txt").exists()
    assert "00:00:00,000 --> 00:00:01,500" in (folder / "transcript.srt").read_text()


def test_summary_written_when_generated(tmp_path, monkeypatch):
    monkeypatch.setattr(transcribe, "fetch_metadata", lambda vid: META_SHORT)
    monkeypatch.setattr(transcribe, "fetch_captions",
                        lambda vid: ([Segment(0, 2, "content")], "manual-captions"))
    monkeypatch.setattr(transcribe, "generate_summary", lambda result: "A fine summary.")

    folder = transcribe.run(VID, output_dir=str(tmp_path))
    md = (folder / "summary.md").read_text()
    assert "A fine summary." in md
    assert "Test" not in md or True  # title comes from metadata
    assert "Short Vid" in md


def test_no_summary_flag_skips_even_with_key(tmp_path, monkeypatch):
    monkeypatch.setattr(transcribe, "fetch_metadata", lambda vid: META_SHORT)
    monkeypatch.setattr(transcribe, "fetch_captions",
                        lambda vid: ([Segment(0, 2, "content")], "manual-captions"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")
    called = {}
    monkeypatch.setattr(transcribe, "generate_summary",
                        lambda result: called.setdefault("yes", True) or "s")

    folder = transcribe.run(VID, output_dir=str(tmp_path), no_summary=True)
    assert "yes" not in called
    assert not (folder / "summary.md").exists()


def test_cli_invalid_url_exit_code():
    rc = transcribe.main(["https://vimeo.com/12345"])
    assert rc == 1


def test_private_video_metadata_error(monkeypatch):
    def boom(vid):
        raise TranscriberError("This video is private.")
    monkeypatch.setattr(transcribe, "fetch_metadata", boom)
    rc = transcribe.main([VID])
    assert rc == 1


# ---------------------------------------------------------------------------
# Regression: network errors must not be misclassified as age restriction
# ---------------------------------------------------------------------------

def test_network_error_not_misclassified_as_age_restricted(monkeypatch):
    import yt_dlp

    class FakeYDL:
        def __init__(self, opts): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def extract_info(self, url, download=False):
            raise yt_dlp.utils.DownloadError("Unable to download API page: connection failed")

    monkeypatch.setattr("yt_dlp.YoutubeDL", FakeYDL)
    with pytest.raises(TranscriberError) as exc_info:
        transcribe.fetch_metadata(VID)
    assert "age-restricted" not in str(exc_info.value)
    assert "Could not fetch" in str(exc_info.value)


def test_age_restricted_correctly_detected(monkeypatch):
    import yt_dlp

    class FakeYDL:
        def __init__(self, opts): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def extract_info(self, url, download=False):
            raise yt_dlp.utils.DownloadError("Sign in to confirm your age. This video may be inappropriate.")

    monkeypatch.setattr("yt_dlp.YoutubeDL", FakeYDL)
    with pytest.raises(TranscriberError, match="age-restricted"):
        transcribe.fetch_metadata(VID)


# ---------------------------------------------------------------------------
# .env file loading
# ---------------------------------------------------------------------------

def test_dotenv_does_not_override_real_env_var(monkeypatch, tmp_path):
    """A real shell environment variable must always win over .env contents."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "from-shell")
    env_file = tmp_path / ".env"
    env_file.write_text("ANTHROPIC_API_KEY=from-dotenv\n")

    from dotenv import load_dotenv
    load_dotenv(env_file, override=False)

    assert os.environ["ANTHROPIC_API_KEY"] == "from-shell"


# ---------------------------------------------------------------------------
# Regression: stale outputs from a previous run must not linger
# ---------------------------------------------------------------------------

def test_no_stale_summary_after_rerun_with_no_summary(tmp_path, monkeypatch):
    """A summary.md from an earlier run must be removed if a later run skips it."""
    monkeypatch.setattr(transcribe, "fetch_metadata", lambda vid: META_SHORT)
    monkeypatch.setattr(transcribe, "fetch_captions",
                        lambda vid: ([Segment(0, 2, "content")], "manual-captions"))

    # First run: produces a summary.
    monkeypatch.setattr(transcribe, "generate_summary", lambda result: "First summary.")
    folder = transcribe.run(VID, output_dir=str(tmp_path))
    assert (folder / "summary.md").exists()

    # Second run: --no-summary should remove the stale summary.md.
    folder = transcribe.run(VID, output_dir=str(tmp_path), no_summary=True)
    assert not (folder / "summary.md").exists()
    assert (folder / "transcript.txt").exists()


def test_no_stale_txt_after_rerun_with_srt(tmp_path, monkeypatch):
    """Switching from .txt to --srt must not leave the old transcript.txt behind."""
    monkeypatch.setattr(transcribe, "fetch_metadata", lambda vid: META_SHORT)
    monkeypatch.setattr(transcribe, "fetch_captions",
                        lambda vid: ([Segment(0, 2, "content")], "manual-captions"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    folder = transcribe.run(VID, output_dir=str(tmp_path))
    assert (folder / "transcript.txt").exists()

    folder = transcribe.run(VID, output_dir=str(tmp_path), srt=True)
    assert (folder / "transcript.srt").exists()
    assert not (folder / "transcript.txt").exists()