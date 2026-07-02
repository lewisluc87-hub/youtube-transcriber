#!/usr/bin/env bash
# Live smoke test — run this on your own machine (needs real YouTube access).
# Exercises every path in the verification plan against real videos.
# Usage: bash smoke_test.sh
set -u

PASS=0; FAIL=0
check() {
  local desc="$1"; local expect_rc="$2"; shift 2
  echo ""
  echo "=== $desc"
  python3 transcribe.py "$@" --no-summary --output-dir smoke-output
  local rc=$?
  if [ "$expect_rc" = "0" ] && [ "$rc" -eq 0 ]; then echo "PASS: $desc"; PASS=$((PASS+1))
  elif [ "$expect_rc" = "nonzero" ] && [ "$rc" -ne 0 ]; then echo "PASS (expected failure): $desc"; PASS=$((PASS+1))
  else echo "FAIL: $desc (exit code $rc)"; FAIL=$((FAIL+1)); fi
}

# 1. Video with manual (creator-uploaded) captions — TED talks reliably have them
check "manual captions" 0 "https://www.youtube.com/watch?v=8S0FDjFBj8o"

# 2. Video with auto-captions only — most casual vlogs; swap in any you know
check "auto captions" 0 "https://www.youtube.com/watch?v=jNQXAC9IVRw"

# 3. Invalid URL — must fail cleanly with exit code 1, no traceback
check "invalid URL" nonzero "https://vimeo.com/12345"

# 4. Malformed/nonexistent video ID — must fail cleanly
check "nonexistent video" nonzero "https://www.youtube.com/watch?v=aaaaaaaaaaa"

# 5. (Optional, slow) No-captions video -> Whisper fallback.
#    Requires: pip install faster-whisper. Uncomment and supply a captionless video URL:
# check "whisper fallback" 0 "https://www.youtube.com/watch?v=YOUR_CAPTIONLESS_VIDEO"

echo ""
echo "=================================="
echo "Results: $PASS passed, $FAIL failed"
echo "Transcripts written to smoke-output/ — open one and confirm text is ordered and readable."
[ "$FAIL" -eq 0 ]
