"""Integration tests for mcp_server.py.

These drive the ACTUAL server as a subprocess over real stdio, using the
official mcp SDK's client -- not a mock of the protocol. transcribe.py's
own network-touching imports (yt_dlp, youtube_transcript_api) are lazy,
so the subprocess's PYTHONPATH is pointed at mcp_test_shims/ (fake
versions of those two libraries, see that directory's docstrings) rather
than mocking anything inside transcribe.py or mcp_server.py themselves.
The real server process runs the real transcribe.py logic (URL parsing,
caption-source preference, output rendering) against fake library calls.

No network access required or attempted -- this sandbox has no route to
YouTube, and these tests don't need one. A separate, explicitly-gated
live smoke test (see smoke_test_mcp.sh) covers the real YouTube path and
was not run as part of building this.
"""

from __future__ import annotations

import sys
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters, stdio_client

ROOT = Path(__file__).parent
SHIMS_DIR = ROOT / "mcp_test_shims"
SERVER_PATH = ROOT / "mcp_server.py"

pytestmark = pytest.mark.asyncio


def _env(scenario: str = "captions", *, output_dir: str | None = None, print_noise: bool = False):
    env = {
        "PYTHONPATH": str(SHIMS_DIR),
        "SHIM_SCENARIO": scenario,
    }
    if output_dir is not None:
        env["YTT_MCP_OUTPUT_DIR"] = output_dir
    if print_noise:
        env["SHIM_PRINT_NOISE"] = "1"
    return env


@asynccontextmanager
async def open_session(env: dict[str, str]):
    """Start the real server as a subprocess (with shims on its
    PYTHONPATH) and open a real MCP session against it over real stdio.

    Deliberately NOT a pytest fixture: mcp's stdio client holds an anyio
    TaskGroup open across the connection's lifetime, and anyio cancel
    scopes are task-bound -- a shared async-generator fixture's teardown
    can run in a different task than its setup under pytest-asyncio,
    which anyio rejects. Opening and closing the session fully within
    each test's own task sidesteps that entirely (same fix as the other
    two MCP servers in this portfolio hit and fixed the same way).
    """
    params = StdioServerParameters(command=sys.executable, args=[str(SERVER_PATH)], env=env)
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        yield session


async def test_lists_all_four_tools():
    async with open_session(_env()) as session:
        tools = await session.list_tools()
        names = {t.name for t in tools.tools}
        assert names == {
            "get_video_info",
            "get_transcript",
            "list_archived_transcripts",
            "read_archived_transcript",
        }


async def test_get_video_info_with_captions():
    async with open_session(_env("captions")) as session:
        res = await session.call_tool(
            "get_video_info", {"url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"}
        )
        text = res.content[0].text
        assert "Shimmed Test Video" in text
        assert "Captions available: True" in text


async def test_get_video_info_no_captions_names_the_cli_fallback():
    async with open_session(_env("no_captions")) as session:
        res = await session.call_tool("get_video_info", {"url": "dQw4w9WgXcQ"})
        text = res.content[0].text
        assert "Captions available: False" in text
        assert "python transcribe.py" in text


@pytest.mark.parametrize("scenario", ["unavailable", "private"])
async def test_unavailable_or_private_video_returns_clean_error_text(scenario):
    # fetch_metadata raises TranscriberError for these; the tool must turn that
    # into readable text rather than crashing the session or leaking a traceback.
    async with open_session(_env(scenario)) as session:
        res = await session.call_tool("get_video_info", {"url": "dQw4w9WgXcQ"})
        text = res.content[0].text
        assert text.startswith("Error:")
        assert "Traceback" not in text
        # ...and the session is still usable afterward.
        again = await session.call_tool("list_archived_transcripts", {})
        assert not again.is_error


async def test_get_transcript_returns_real_shimmed_captions():
    async with open_session(_env("captions")) as session:
        res = await session.call_tool("get_transcript", {"url": "dQw4w9WgXcQ"})
        text = res.content[0].text
        assert "Untrusted transcript text" in text
        assert "Hello and welcome to this shimmed video." in text
        assert "No real YouTube request was made." in text


async def test_get_transcript_pagination_offsets_correctly():
    async with open_session(_env("captions")) as session:
        page1 = await session.call_tool(
            "get_transcript", {"url": "dQw4w9WgXcQ", "max_chars": 50}
        )
        text1 = page1.content[0].text
        assert "next_offset=50" in text1

        page2 = await session.call_tool(
            "get_transcript", {"url": "dQw4w9WgXcQ", "offset": 50, "max_chars": 50}
        )
        text2 = page2.content[0].text
        assert "chars 50-100" in text2
        # No overlap: page 2 shouldn't repeat page 1's opening line.
        assert "Hello and welcome" not in text2


async def test_get_transcript_no_captions_does_not_invoke_whisper():
    async with open_session(_env("no_captions")) as session:
        res = await session.call_tool("get_transcript", {"url": "dQw4w9WgXcQ"})
        text = res.content[0].text
        assert "No captions available" in text
        assert "Whisper" in text  # points at the CLI, doesn't silently hang


async def test_get_video_info_rejects_non_youtube_url():
    async with open_session(_env()) as session:
        res = await session.call_tool("get_video_info", {"url": "https://evil.com/watch?v=dQw4w9WgXcQ"})
        assert res.is_error


async def test_stray_print_from_shimmed_library_does_not_corrupt_the_session():
    # The whole point of _protect_stdio(): a fake library print()s mid-call
    # (SHIM_PRINT_NOISE=1) and the session must survive and return the
    # correct result anyway, proving the stdio JSON-RPC stream wasn't
    # corrupted by the stray stdout write.
    async with open_session(_env("captions", print_noise=True)) as session:
        res = await session.call_tool(
            "get_video_info", {"url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"}
        )
        assert "Shimmed Test Video" in res.content[0].text
        # And the session is still usable for a second call afterward --
        # a corrupted stream would typically break everything after it.
        res2 = await session.call_tool("get_transcript", {"url": "dQw4w9WgXcQ"})
        assert "Hello and welcome" in res2.content[0].text


async def test_list_archived_transcripts_reports_real_seeded_archive():
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp) / "dQw4w9WgXcQ-some-title"
        folder.mkdir()
        (folder / "transcript.txt").write_text("[00:00:00] hi\n")

        async with open_session(_env(output_dir=tmp)) as session:
            res = await session.call_tool("list_archived_transcripts", {})
            text = res.content[0].text
            assert "dQw4w9WgXcQ" in text
            assert "transcript.txt=True" in text
            assert "transcript.srt=False" in text


async def test_read_archived_transcript_returns_real_file_contents():
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp) / "dQw4w9WgXcQ-some-title"
        folder.mkdir()
        (folder / "transcript.txt").write_text("[00:00:00] archived hello\n")

        async with open_session(_env(output_dir=tmp)) as session:
            res = await session.call_tool("read_archived_transcript", {"video_id": "dQw4w9WgXcQ"})
            text = res.content[0].text
            assert "archived hello" in text
            assert "Untrusted transcript text" in text


async def test_read_archived_transcript_rejects_path_traversal():
    with tempfile.TemporaryDirectory() as tmp:
        async with open_session(_env(output_dir=tmp)) as session:
            res = await session.call_tool(
                "read_archived_transcript", {"video_id": "../../../etc/passwd"}
            )
            assert res.is_error


async def test_list_archived_transcripts_on_empty_directory():
    with tempfile.TemporaryDirectory() as tmp:
        async with open_session(_env(output_dir=tmp)) as session:
            res = await session.call_tool("list_archived_transcripts", {})
            assert "no transcribed videos" in res.content[0].text.lower()
