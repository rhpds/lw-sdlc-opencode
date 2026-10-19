#!/usr/bin/env python3
"""Summarize an OpenCode GET /session/{id}/message payload for AAP/SNOW.

Reads JSON (list of messages) from stdin. Prints a short plain-text summary
to stdout (last assistant text, truncated). Tools used are listed on stderr
as a one-liner when present.

Usage:
  curl …/message | python3 opencode_session_summary.py [--max 1200]
"""
from __future__ import annotations

import argparse
import json
import sys


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max", type=int, default=1200, help="max summary chars")
    args = ap.parse_args()
    try:
        msgs = json.load(sys.stdin)
    except json.JSONDecodeError as e:
        print(f"(could not parse OpenCode messages: {e})", file=sys.stderr)
        return 1
    if not isinstance(msgs, list):
        print("(unexpected OpenCode message payload)", file=sys.stderr)
        return 1

    tools: list[str] = []
    assistant_chunks: list[str] = []
    for m in msgs:
        if not isinstance(m, dict):
            continue
        role = (m.get("info") or {}).get("role") or ""
        for p in m.get("parts") or []:
            if not isinstance(p, dict):
                continue
            ptype = p.get("type") or ""
            if ptype == "tool":
                name = p.get("tool") or p.get("name") or "tool"
                if name not in tools:
                    tools.append(str(name))
            if role == "assistant" and ptype == "text" and p.get("text"):
                t = str(p["text"]).strip()
                if t:
                    assistant_chunks.append(t)

    if tools:
        print("tools: " + ", ".join(tools[:12]), file=sys.stderr)

    if not assistant_chunks:
        print("(no assistant text in session yet)")
        return 0

    text = assistant_chunks[-1]
    # Prefer the last non-trivial chunk if the final one is tiny.
    for chunk in reversed(assistant_chunks):
        if len(chunk) >= 40:
            text = chunk
            break

    if len(text) > args.max:
        text = text[: args.max - 1].rstrip() + "…"
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
