#!/usr/bin/env python3
"""Measure how large a string can cross Chrome's `execute javascript` boundary.

WHY THIS EXISTS
---------------
`BrowserSessionExecutor` fetches query results by having a Chrome tab stringify
the whole result set into `window.__mb.payload`, then reading that string back
through AppleScript. `PolicySettings.max_transport_rows` (50,000) is a guess at
what that channel carries; nothing has ever measured it, and the dangerous
failure is a SILENT one -- a truncated payload that still parses.

Two legs could truncate:
  1. AppleScript -> osascript stdout.  Measured: 20 MB passes intact. Not it.
  2. Chrome tab  -> AppleScript (an AppleEvent). UNMEASURED. This script.

WHAT IT DOES
------------
Builds a string of N characters INSIDE the tab (no network, no Metabase, no
page data touched) and checks how many characters came back. It never reads the
page, never sends anything anywhere, and leaves no state behind.

USAGE
-----
Open a Chrome tab on any harmless page (about:blank is ideal), then:

    .venv/bin/python scripts/measure_applescript_ceiling.py

Point it at a specific tab instead of the front one with:

    .venv/bin/python scripts/measure_applescript_ceiling.py --host localhost:3000

READING THE RESULT
------------------
"intact" at size N means the channel carried N characters.
"TRUNCATED" is the important line: it means the boundary silently dropped
characters -- exactly the failure mode max_transport_rows exists to avoid.

Rough conversion: one row of a typical 6-column cube serialises to ~80-120
bytes of JSON. Divide the largest intact size by ~120 for a conservative row
estimate, then set ANALYTICS_MAX_TRANSPORT_ROWS well below it.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys

SIZES = [100_000, 500_000, 1_000_000, 2_000_000, 5_000_000, 10_000_000, 20_000_000]


def build_command(js: str, host: str = "") -> list:
    """Same AppleScript shape browser_session.build_osascript_command emits, so
    this measures the real channel rather than an approximation of it."""
    quoted = json.dumps(js)  # AppleScript string literals match JSON's escaping
    if host:
        safe_host = host.replace('"', "")
        body = (
            "repeat with w in windows\n"
            "  repeat with t in tabs of w\n"
            f'    if URL of t contains "{safe_host}" then\n'
            f"      return (execute t javascript {quoted})\n"
            "    end if\n"
            "  end repeat\n"
            "end repeat\n"
            f"return (execute front window's active tab javascript {quoted})\n"
        )
    else:
        body = f"return (execute front window's active tab javascript {quoted})\n"
    return ["osascript", "-e", f'tell application "Google Chrome"\n{body}\nend tell']


def measure(size: int, host: str, timeout_s: float = 60.0) -> tuple:
    # Built in the tab, so the string never crosses any other boundary first.
    js = f"'x'.repeat({size})"
    try:
        out = subprocess.run(build_command(js, host), capture_output=True,
                             text=True, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        return 0, "TIMED OUT"
    if out.returncode != 0:
        return 0, f"ERROR: {(out.stderr or '').strip()[:200]}"
    got = len(out.stdout.rstrip("\n"))
    if got == size:
        return got, "intact"
    return got, f"TRUNCATED -- asked {size:,}, got {got:,}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="",
                    help="URL fragment identifying the tab (default: front tab)")
    args = ap.parse_args()

    print("Measuring the Chrome -> AppleScript string channel.\n")
    largest_intact = 0
    for size in SIZES:
        got, verdict = measure(size, args.host)
        print(f"  {size:>12,} chars -> {verdict}")
        if verdict == "intact":
            largest_intact = got
        else:
            break

    print()
    if not largest_intact:
        print("Nothing came back. Is Chrome running with a window open?")
        return 1
    if largest_intact == SIZES[-1]:
        print(f"No ceiling found up to {largest_intact:,} chars. The transport is not "
              f"the constraint it was assumed to be -- see the note in config.py.")
    else:
        print(f"Largest intact payload: {largest_intact:,} chars.")
    rows = largest_intact // 120
    print(f"At ~120 bytes per cube row that is roughly {rows:,} rows.")
    print(f"Set ANALYTICS_MAX_TRANSPORT_ROWS BELOW that, with margin -- "
          f"{int(rows * 0.5):,} would be a conservative choice.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
