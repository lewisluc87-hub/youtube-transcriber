"""Fake youtube_transcript_api for MCP server integration tests.

Real transcribe.py does:
    api = YouTubeTranscriptApi()
    transcript_list = api.list(video_id)
    transcript = transcript_list.find_manually_created_transcript([...])
    fetched = transcript.fetch()  # iterable of objects with .start/.duration/.text

This shim reproduces that surface with three fake caption segments and no
real network call, controlled by the same SHIM_SCENARIO env var as the
yt_dlp shim (see that module's docstring for the scenario list).
"""

from __future__ import annotations

import os

from ._errors import NoTranscriptFound, TranscriptsDisabled, VideoUnavailable

_FAKE_SEGMENTS = [
    {"start": 0.0, "duration": 2.5, "text": "Hello and welcome to this shimmed video."},
    {"start": 2.5, "duration": 3.0, "text": "This transcript is entirely fake test data."},
    {"start": 5.5, "duration": 2.0, "text": "No real YouTube request was made."},
]


class _FetchedItem:
    def __init__(self, d):
        self.start = d["start"]
        self.duration = d["duration"]
        self.text = d["text"]


class _Transcript:
    def __init__(self, language_code="en"):
        self.language_code = language_code

    def fetch(self):
        return [_FetchedItem(d) for d in _FAKE_SEGMENTS]


class _TranscriptList:
    def __init__(self):
        self._en = _Transcript("en")

    def __iter__(self):
        return iter([self._en])

    def find_manually_created_transcript(self, language_codes):
        if "en" in language_codes:
            return self._en
        raise NoTranscriptFound("video", language_codes, self)

    def find_generated_transcript(self, language_codes):
        raise NoTranscriptFound("video", language_codes, self)


class YouTubeTranscriptApi:
    def list(self, video_id):
        scenario = os.environ.get("SHIM_SCENARIO", "captions")

        if os.environ.get("SHIM_PRINT_NOISE"):
            print("[shim] pretending to fetch captions...")

        if scenario == "no_captions":
            raise TranscriptsDisabled(video_id)
        if scenario in ("unavailable", "private"):
            raise VideoUnavailable(video_id)
        return _TranscriptList()
