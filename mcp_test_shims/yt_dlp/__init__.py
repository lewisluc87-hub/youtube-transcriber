"""Fake yt_dlp for MCP server integration tests.

Real transcribe.py does `import yt_dlp` then `yt_dlp.YoutubeDL(opts)` as a
context manager with `.extract_info(url, download=False)`, and catches
`yt_dlp.utils.DownloadError`. This shim reproduces exactly that surface
with no real network call, controlled by the SHIM_SCENARIO env var so
different tests can select different fake videos without needing
separate shim files.

Scenarios (set SHIM_SCENARIO before launching the server subprocess):
  "captions"      -> normal short video, has captions
  "no_captions"   -> normal short video, no captions
  "unavailable"   -> metadata fetch raises "video unavailable"
  "private"       -> metadata fetch raises "private video"
"""

from __future__ import annotations

import os

from . import utils  # re-exported so `yt_dlp.utils.DownloadError` works


class YoutubeDL:
    def __init__(self, opts=None):
        self.opts = opts or {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def extract_info(self, url, download=False):
        scenario = os.environ.get("SHIM_SCENARIO", "captions")

        if os.environ.get("SHIM_PRINT_NOISE"):
            # Deliberately prints during a tool call, to test that the MCP
            # server protects the stdio JSON-RPC transport from stray
            # stdout writes made by code it doesn't control.
            print("[shim] pretending to talk to YouTube...")

        if scenario == "unavailable":
            raise utils.DownloadError("ERROR: [youtube] video: This video is unavailable")
        if scenario == "private":
            raise utils.DownloadError("ERROR: [youtube] video: Private video. Sign in if you've been invited.")

        return {
            "title": "Shimmed Test Video",
            "channel": "Shim Channel",
            "uploader": "Shim Channel",
            "duration": 125,
        }

    def download(self, urls):
        # Whisper's audio-download path is intentionally not exercised by
        # the MCP server (see README: Whisper fallback not exposed via
        # MCP) -- not needed by any MCP server test.
        raise NotImplementedError("audio download is not used by the MCP server")
