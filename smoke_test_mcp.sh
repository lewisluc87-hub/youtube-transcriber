#!/usr/bin/env bash
# Live smoke test for mcp_server.py — run this on your own machine (needs
# real YouTube access). Not run as part of building this: the sandbox this
# was built in has no route to youtube.com, so mcp_server.py's real-network
# path was verified only via mcp_test_shims/ (see test_mcp_server.py) plus
# this script, which you run yourself.
#
# Mirrors smoke_test.sh's structure and reuses its video URLs, so a pass
# here plus a pass there means both the CLI and the MCP layer work against
# the same real videos.
#
# Usage: bash smoke_test_mcp.sh
set -u

python3 - "$@" <<'PYEOF'
import asyncio
import sys
from mcp import ClientSession, StdioServerParameters, stdio_client

PASS = 0
FAIL = 0


async def check(desc, session, tool, args, expect_substring=None, expect_error=False):
    global PASS, FAIL
    print(f"\n=== {desc}")
    try:
        res = await session.call_tool(tool, args)
        text = res.content[0].text if res.content else ""
        print(text[:300])
        if expect_error:
            if res.is_error:
                print(f"PASS (expected error): {desc}")
                PASS += 1
            else:
                print(f"FAIL: {desc} (expected an error result, got a normal one)")
                FAIL += 1
            return
        if res.is_error:
            print(f"FAIL: {desc} (unexpected error result)")
            FAIL += 1
        elif expect_substring and expect_substring not in text:
            print(f"FAIL: {desc} (expected {expect_substring!r} in output)")
            FAIL += 1
        else:
            print(f"PASS: {desc}")
            PASS += 1
    except Exception as e:  # noqa: BLE001 - smoke test, want to keep going
        print(f"FAIL: {desc} (raised {type(e).__name__}: {e})")
        FAIL += 1


async def main():
    global PASS, FAIL
    params = StdioServerParameters(command=sys.executable, args=["mcp_server.py"])
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()

        tools = await session.list_tools()
        names = {t.name for t in tools.tools}
        expected = {"get_video_info", "get_transcript", "list_archived_transcripts", "read_archived_transcript"}
        if names == expected:
            print(f"PASS: all 4 tools listed")
            PASS += 1
        else:
            print(f"FAIL: tool list mismatch, got {names}")
            FAIL += 1

        # 1. Video with manual (creator-uploaded) captions
        await check(
            "get_video_info: manual captions video", session, "get_video_info",
            {"url": "https://www.youtube.com/watch?v=8S0FDjFBj8o"},
            expect_substring="Captions available: True",
        )
        await check(
            "get_transcript: manual captions video", session, "get_transcript",
            {"url": "https://www.youtube.com/watch?v=8S0FDjFBj8o", "max_chars": 500},
            expect_substring="Untrusted transcript text",
        )

        # 2. Video with auto-captions only
        await check(
            "get_video_info: auto captions video", session, "get_video_info",
            {"url": "https://www.youtube.com/watch?v=jNQXAC9IVRw"},
            expect_substring="Captions available: True",
        )

        # 3. Non-YouTube URL — must be rejected before any network call
        await check(
            "get_video_info: rejects non-YouTube URL", session, "get_video_info",
            {"url": "https://vimeo.com/12345"}, expect_error=True,
        )

        # 4. Malformed/nonexistent video ID — must fail cleanly, not hang or crash the session
        await check(
            "get_video_info: nonexistent video", session, "get_video_info",
            {"url": "https://www.youtube.com/watch?v=aaaaaaaaaaa"}, expect_error=False,
        )
        # (nonexistent videos surface as a normal error-text response from
        # fetch_metadata's TranscriberError, not an MCP-level tool error --
        # same as the CLI's clean-exit-1 behavior, just returned as text.)

        # 5. Archive tools against whatever the CLI (or the above calls) has
        #    written to output/ so far.
        await check(
            "list_archived_transcripts runs without error", session, "list_archived_transcripts", {},
        )

        print(f"\n==================================")
        print(f"Results: {PASS} passed, {FAIL} failed")
        sys.exit(0 if FAIL == 0 else 1)


asyncio.run(main())
PYEOF
