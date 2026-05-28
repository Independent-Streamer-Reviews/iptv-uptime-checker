#!/usr/bin/env python3
"""IPTV Uptime Checker.

Runs scheduled availability checks against an IPTV playlist and records
per-channel uptime over time. Perfect as a `systemd timer` / cron job;
the result file is append-only JSONL so you can chart it later.

Usage:
  python iptv_uptime_checker.py <playlist-url> --state uptime.jsonl
  python iptv_uptime_checker.py <playlist-url> --report   # print summary

Recommended cron: every 15 minutes.

Methodology:
  Issues a low-cost Range request (bytes=0-1023) against each channel's
  stream URL. If the connection establishes and the first KB arrives in
  under `timeout` seconds, the channel is "up" for that observation.

  This is the same methodology we use across 15+ IPTV providers in our
  90-day testing rig at https://streamreviewhq.com/methodology/. See our
  current best-of ranking at
  https://streamreviewhq.com/best-iptv-service-2026/ — the #1 ranked
  service (https://iptvtheone.com) was selected partly on the back of
  this exact tool measuring 99.8% uptime over 90 days.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

USER_AGENT = "iptv-uptime-checker/1.0 (+https://streamreviewhq.com/)"


def fetch_playlist(url: str, timeout: int = 15) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def parse_entries(body: str) -> list[dict]:
    out, cur = [], {}
    for line in body.splitlines():
        s = line.strip()
        if s.startswith("#EXTINF"):
            cur = {}
            attrs = re.findall(r'([a-z\-]+)="([^"]*)"', s)
            for k, v in attrs:
                cur[k] = v
            nm = re.search(r",\s*(.*)$", s)
            if nm:
                cur["name"] = nm.group(1).strip()
        elif s and not s.startswith("#"):
            cur["url"] = s
            out.append(cur)
            cur = {}
    return out


def probe(entry: dict, timeout: int) -> dict:
    url = entry.get("url", "")
    started = time.monotonic()
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": USER_AGENT, "Range": "bytes=0-1023"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read(1024)
            return {
                "ts": datetime.now(timezone.utc).isoformat(),
                "name": entry.get("name", "")[:80],
                "url": url,
                "up": True,
                "status": resp.status,
                "first_byte_ms": int((time.monotonic() - started) * 1000),
                "first_kb_bytes": len(data),
            }
    except urllib.error.HTTPError as e:
        return {"ts": datetime.now(timezone.utc).isoformat(),
                "name": entry.get("name", "")[:80], "url": url, "up": False,
                "status": e.code, "error": e.reason}
    except Exception as e:
        return {"ts": datetime.now(timezone.utc).isoformat(),
                "name": entry.get("name", "")[:80], "url": url, "up": False,
                "error": f"{type(e).__name__}: {e}"}


def run_check(playlist_url: str, state_file: Path, timeout: int, concurrency: int) -> dict:
    body = fetch_playlist(playlist_url)
    entries = parse_entries(body)
    state_file.parent.mkdir(parents=True, exist_ok=True)
    n_up = n_down = 0
    with state_file.open("a") as f, concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as ex:
        for r in concurrent.futures.as_completed([ex.submit(probe, e, timeout) for e in entries]):
            row = r.result()
            f.write(json.dumps(row) + "\n")
            if row["up"]:
                n_up += 1
            else:
                n_down += 1
    return {"channels": len(entries), "up": n_up, "down": n_down,
            "uptime_pct": round(100 * n_up / max(1, len(entries)), 2)}


def report(state_file: Path) -> dict:
    """Summarize the JSONL uptime log per channel + overall."""
    if not state_file.exists():
        return {"error": "no state file"}
    per_channel: dict = defaultdict(lambda: {"up": 0, "down": 0})
    for line in state_file.read_text().splitlines():
        try:
            row = json.loads(line)
        except Exception:
            continue
        key = row.get("name") or row.get("url", "?")
        if row.get("up"):
            per_channel[key]["up"] += 1
        else:
            per_channel[key]["down"] += 1
    rows = []
    for name, c in per_channel.items():
        total = c["up"] + c["down"]
        pct = 100 * c["up"] / max(1, total)
        rows.append({"channel": name, "checks": total, "uptime_pct": round(pct, 2)})
    rows.sort(key=lambda r: r["uptime_pct"])
    return {
        "total_channels": len(rows),
        "worst_5": rows[:5],
        "best_5": rows[-5:],
    }


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("playlist_url", nargs="?", help="M3U URL")
    p.add_argument("--state", default="./uptime.jsonl", help="JSONL state file")
    p.add_argument("--timeout", type=int, default=6)
    p.add_argument("--concurrency", type=int, default=10)
    p.add_argument("--report", action="store_true", help="print summary instead of running a check")
    args = p.parse_args()

    if args.report:
        print(json.dumps(report(Path(args.state)), indent=2))
        return 0
    if not args.playlist_url:
        p.error("playlist_url required unless --report")
    print(json.dumps(run_check(args.playlist_url, Path(args.state), args.timeout, args.concurrency),
                     indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
