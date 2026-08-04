#!/usr/bin/env python3
"""
tools_verify_dispatch.py
Read back what the dispatcher actually did, for the messages you just sent.

Usage:  python3 tools_verify_dispatch.py [N]      # N = how many recent, default 12

Pairs each dispatch_log row with the llm_exchanges row it produced, so you can
see the selection and what the real call cost in one place. Read-only.
"""

import json
import sqlite3
import sys

import paths


def main(limit: int = 12) -> None:
    conn = sqlite3.connect(f"file:{paths.db_path()}?mode=ro", uri=True)
    rows = conn.execute(
        "SELECT id, created_at, raw_message, selected_tools, outcome, "
        "       latency_ms, tokens_in, fallback_triggered "
        "FROM dispatch_log ORDER BY id DESC LIMIT ?", (limit,),
    ).fetchall()
    if not rows:
        print("dispatch_log is empty — send a message to Friday first.")
        return

    print(f"{'when':9s} {'message':32s} {'selected':34s} {'disp':>6s} {'chat':>6s}  flags")
    print("-" * 104)
    for (_id, at, raw, tools_json, outcome, ms, dtok, fb) in reversed(rows):
        tools = json.loads(tools_json or "[]")
        # The chat call that followed this dispatch: nearest user_message
        # exchange at or after it. Ordering is safe because the semaphore
        # serializes the whole path.
        hit = conn.execute(
            "SELECT tokens_in FROM llm_exchanges "
            "WHERE triggered_by='user_message' AND timestamp >= ? "
            "ORDER BY timestamp LIMIT 1", (at,),
        ).fetchone()
        shown = ", ".join(t.replace("_calendar", "_cal") for t in tools) or "— none —"
        if len(tools) == 10:
            shown = "ALL (fail-open)"
        flags = []
        if outcome != "ok":
            flags.append(outcome.upper())
        if fb:
            flags.append("FALLBACK-RETRY")
        print(f"{at[11:19]:9s} {raw[:31]:32s} {shown[:33]:34s} "
              f"{ms:5d}m {(hit[0] if hit else 0):6d}  {' '.join(flags)}")

    print()
    tot, fbs = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(fallback_triggered),0) FROM dispatch_log"
    ).fetchone()
    dupes = conn.execute(
        "SELECT COUNT(*) FROM (SELECT 1 FROM dispatch_log "
        "GROUP BY message_hash HAVING COUNT(*) > 1)"
    ).fetchone()[0]
    print(f"all time: {tot} dispatches, {fbs} fallback retries "
          f"({100.0 * fbs / tot:.1f}%), {dupes} repeated messages")


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 12)
