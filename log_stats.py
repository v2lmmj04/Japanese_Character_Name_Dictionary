#!/usr/bin/env python3
"""Parse a Caddy JSON access log and print usage stats for the Yomitan dict builder."""

import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse, parse_qs

DEFAULT_LOG = Path(__file__).resolve().parent / "access.log"

# All toggleable features and their defaults (all default to true)
FEATURES = ["honorifics", "image", "tag", "description", "traits", "spoilers", "seiyuu"]

# API paths that actually generate dictionaries (where feature params matter)
DICT_PATHS = {"/api/yomitan-dict", "/api/generate-stream", "/api/yomitan-index"}


def parse_log(path: Path) -> list[dict]:
    entries = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return entries


def ts_to_dt(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, tz=timezone.utc)


def fmt_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def sec(title: str):
    print(f"\n{'=' * 64}")
    print(f"  {title}")
    print(f"{'=' * 64}")


def bar_chart(counter: Counter, limit: int = 25):
    if not counter:
        print("  (none)")
        return
    total = sum(counter.values())
    max_label = max(len(str(k)) for k in counter)
    for key, count in counter.most_common(limit):
        pct = count / total * 100
        bar = "█" * max(1, int(pct / 2))
        print(f"  {str(key):<{max_label}}  {count:>6}  ({pct:5.1f}%)  {bar}")


def main():
    log_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_LOG
    if not log_path.exists():
        print(f"Error: {log_path} not found", file=sys.stderr)
        sys.exit(1)

    entries = parse_log(log_path)
    if not entries:
        print("No log entries found.")
        sys.exit(0)

    # ── Accumulators ────────────────────────────────────────────
    statuses = Counter()
    ips = Counter()
    browsers = Counter()
    platforms = Counter()
    paths_counter = Counter()
    hours = Counter()
    durations: list[float] = []
    sizes: list[int] = []

    # Usernames (extracted from query params)
    vndb_usernames = Counter()       # username → request count
    anilist_usernames = Counter()
    vndb_user_ips: dict[str, set[str]] = defaultdict(set)  # username → set of IPs
    anilist_user_ips: dict[str, set[str]] = defaultdict(set)

    # IP-level source tracking
    anilist_ips: set[str] = set()
    vndb_ips: set[str] = set()
    user_based_ips: set[str] = set()

    # Manual (source+id, no username)
    manual_reqs = 0
    manual_ips: set[str] = set()
    manual_sources = Counter()
    manual_ids = Counter()

    # Feature toggles (only on dict-generation endpoints)
    # Count how many times each feature was explicitly turned OFF
    feature_off_count = Counter()
    # Count how many dict requests had each feature ON (default or explicit)
    feature_on_count = Counter()
    dict_gen_reqs = 0  # total dict-generation requests (denominator)

    # Media type usage (AniList)
    media_types = Counter()

    # Per-endpoint breakdown
    endpoint_counter = Counter()

    for e in entries:
        req = e.get("request", {})
        headers = req.get("headers", {})
        uri = req.get("uri", "")
        remote_ip = req.get("remote_ip", "")

        parsed = urlparse(uri)
        path = parsed.path
        qs = parse_qs(parsed.query)

        statuses[e.get("status", 0)] += 1
        ips[remote_ip] += 1
        paths_counter[path] += 1
        endpoint_counter[path] += 1

        # ── Browser / platform ──────────────────────────────────
        ua_raw = (headers.get("User-Agent") or [""])[0]
        if ua_raw:
            if "Chrome" in ua_raw and "Edg" not in ua_raw:
                browsers["Chrome"] += 1
            elif "Firefox" in ua_raw:
                browsers["Firefox"] += 1
            elif "Safari" in ua_raw and "Chrome" not in ua_raw:
                browsers["Safari"] += 1
            elif "Edg" in ua_raw:
                browsers["Edge"] += 1
            else:
                browsers[ua_raw[:50]] += 1

        plat = (headers.get("Sec-Ch-Ua-Platform") or [""])[0].strip('"')
        if plat:
            platforms[plat] += 1

        # ── Timing ──────────────────────────────────────────────
        ts = e.get("ts")
        if ts:
            hours[ts_to_dt(ts).strftime("%Y-%m-%d %H:00")] += 1
        dur = e.get("duration")
        if dur is not None:
            durations.append(dur)
        sz = e.get("size")
        if sz is not None:
            sizes.append(sz)

        # ── Username extraction ─────────────────────────────────
        has_vndb_user = "vndb_user" in qs
        has_anilist_user = "anilist_user" in qs
        has_source = "source" in qs
        has_id = "id" in qs

        if has_vndb_user:
            uname = qs["vndb_user"][0]
            vndb_usernames[uname] += 1
            vndb_user_ips[uname].add(remote_ip)
            vndb_ips.add(remote_ip)
            user_based_ips.add(remote_ip)

        if has_anilist_user:
            uname = qs["anilist_user"][0]
            anilist_usernames[uname] += 1
            anilist_user_ips[uname].add(remote_ip)
            anilist_ips.add(remote_ip)
            user_based_ips.add(remote_ip)

        # ── Manual download (source+id, no username) ────────────
        is_manual = has_source and has_id and not has_vndb_user and not has_anilist_user
        if is_manual:
            manual_reqs += 1
            manual_ips.add(remote_ip)
            src = qs.get("source", ["?"])[0]
            mid = qs.get("id", ["?"])[0]
            manual_sources[src] += 1
            manual_ids[f"{src}:{mid}"] += 1

        # ── Feature toggle tracking (dict endpoints only) ───────
        if path in DICT_PATHS:
            dict_gen_reqs += 1
            for feat in FEATURES:
                val = qs.get(feat, ["true"])[0].lower()
                if val == "false":
                    feature_off_count[feat] += 1
                else:
                    feature_on_count[feat] += 1

        # ── Media type ──────────────────────────────────────────
        if "media_type" in qs:
            media_types[qs["media_type"][0].upper()] += 1

    # ── Derived stats ───────────────────────────────────────────
    timestamps = [e["ts"] for e in entries if "ts" in e]
    first = ts_to_dt(min(timestamps))
    last = ts_to_dt(max(timestamps))
    span = (last - first).total_seconds()
    total_ips = len(ips)
    one_time = len([ip for ip, c in ips.items() if c == 1])
    repeat = total_ips - one_time

    # ════════════════════════════════════════════════════════════
    #  PRINT REPORT
    # ════════════════════════════════════════════════════════════

    sec("OVERVIEW")
    print(f"  Log file       : {log_path}")
    print(f"  Total requests : {len(entries)}")
    print(f"  Time range     : {first:%Y-%m-%d %H:%M} → {last:%Y-%m-%d %H:%M} UTC")
    if span > 0:
        print(f"  Span           : {span / 3600:.1f} hours ({span / 86400:.1f} days)")
        print(f"  Req/hour       : {len(entries) / (span / 3600):.1f}")
    print(f"  Unique IPs     : {total_ips}")
    print(f"    One-time     : {one_time}")
    print(f"    Repeat (2+)  : {repeat}")

    # ── 1. VNDB Usernames ───────────────────────────────────────
    sec("VNDB USERNAMES")
    if vndb_usernames:
        max_u = max(len(u) for u in vndb_usernames)
        for uname, cnt in vndb_usernames.most_common():
            n_ips = len(vndb_user_ips[uname])
            print(f"  {uname:<{max_u}}  {cnt:>4} reqs  ({n_ips} IP{'s' if n_ips != 1 else ''})")
    else:
        print("  (none)")

    # ── 2. AniList Usernames ────────────────────────────────────
    sec("ANILIST USERNAMES")
    if anilist_usernames:
        max_u = max(len(u) for u in anilist_usernames)
        for uname, cnt in anilist_usernames.most_common():
            n_ips = len(anilist_user_ips[uname])
            print(f"  {uname:<{max_u}}  {cnt:>4} reqs  ({n_ips} IP{'s' if n_ips != 1 else ''})")
    else:
        print("  (none)")

    # ── 3. Visit counts ────────────────────────────────────────
    sec("VISITS & TRAFFIC")
    print(f"  Total requests           : {len(entries)}")
    print(f"  Unique visitors (IPs)    : {total_ips}")
    print(f"  One-time visitors        : {one_time}")
    print(f"  Returning visitors (2+)  : {repeat}")
    if repeat > 0:
        repeat_counts = sorted(
            [(ip, c) for ip, c in ips.items() if c > 1],
            key=lambda x: -x[1],
        )
        print(f"  Top returning visitors:")
        for ip, c in repeat_counts[:10]:
            print(f"    {ip:<20} {c:>5} requests")

    # ── 4 & 5. AniList vs VNDB usage ───────────────────────────
    sec("PLATFORM USAGE: ANILIST vs VNDB")
    total_user_reqs = sum(vndb_usernames.values()) + sum(anilist_usernames.values())
    print(f"  AniList")
    print(f"    Requests       : {sum(anilist_usernames.values())}")
    print(f"    Unique users   : {len(anilist_usernames)}")
    print(f"    Unique IPs     : {len(anilist_ips)}")
    print(f"  VNDB")
    print(f"    Requests       : {sum(vndb_usernames.values())}")
    print(f"    Unique users   : {len(vndb_usernames)}")
    print(f"    Unique IPs     : {len(vndb_ips)}")
    both_platform_ips = anilist_ips & vndb_ips
    if both_platform_ips:
        print(f"  IPs using BOTH   : {len(both_platform_ips)}")
    if media_types:
        print(f"  AniList media_type breakdown:")
        for mt, cnt in media_types.most_common():
            print(f"    {mt:<10} : {cnt}")

    # ── 6. Manual vs Username-based ─────────────────────────────
    sec("MANUAL vs USERNAME-BASED")
    print(f"  Username-based requests  : {total_user_reqs}")
    print(f"  Username-based IPs       : {len(user_based_ips)}")
    print(f"  Manual (source+id) reqs  : {manual_reqs}")
    print(f"  Manual IPs               : {len(manual_ips)}")
    manual_only = manual_ips - user_based_ips
    user_only = user_based_ips - manual_ips
    both_mode = manual_ips & user_based_ips
    print(f"  IPs using only username  : {len(user_only)}")
    print(f"  IPs using only manual    : {len(manual_only)}")
    print(f"  IPs using both modes     : {len(both_mode)}")
    if manual_sources:
        print(f"  Manual by source:")
        for src, cnt in manual_sources.most_common():
            print(f"    {src:<10} : {cnt}")
    if manual_ids:
        print(f"  Top manual media IDs:")
        for mid, cnt in manual_ids.most_common(20):
            print(f"    {mid:<25} : {cnt}")

    # ── 7 & 8. Feature usage ───────────────────────────────────
    sec("FEATURE TOGGLES (dict-generation endpoints only)")
    if dict_gen_reqs == 0:
        print("  No dict-generation requests found.")
    else:
        print(f"  Total dict-generation requests: {dict_gen_reqs}")
        print(f"  (All features default to ON — only explicitly disabled ones show as OFF)\n")

        # Table header
        print(f"  {'Feature':<14} {'ON':>6} {'OFF':>6} {'% ON':>7} {'% OFF':>7}")
        print(f"  {'─' * 14} {'─' * 6} {'─' * 6} {'─' * 7} {'─' * 7}")
        for feat in FEATURES:
            on = feature_on_count.get(feat, 0)
            off = feature_off_count.get(feat, 0)
            pct_on = on / dict_gen_reqs * 100 if dict_gen_reqs else 0
            pct_off = off / dict_gen_reqs * 100 if dict_gen_reqs else 0
            print(f"  {feat:<14} {on:>6} {off:>6} {pct_on:>6.1f}% {pct_off:>6.1f}%")

        print()
        most_disabled = feature_off_count.most_common()
        if most_disabled:
            print("  Most turned OFF:")
            for feat, cnt in most_disabled:
                print(f"    {feat:<14} disabled {cnt} times ({cnt / dict_gen_reqs * 100:.1f}%)")
        else:
            print("  No features were explicitly disabled in any request.")

        never_disabled = [f for f in FEATURES if feature_off_count.get(f, 0) == 0]
        if never_disabled:
            print(f"  Always left ON : {', '.join(never_disabled)}")

    # ── Server stats (condensed) ────────────────────────────────
    sec("ENDPOINTS")
    bar_chart(endpoint_counter)

    sec("STATUS CODES")
    bar_chart(statuses)

    sec("BROWSERS")
    bar_chart(browsers)

    sec("PLATFORMS")
    bar_chart(platforms)

    sec("TRAFFIC BY HOUR")
    if hours:
        # Show top 20 busiest hours
        bar_chart(hours, limit=20)

    if durations:
        durations.sort()
        sec("RESPONSE TIME (seconds)")
        n = len(durations)
        print(f"  Min    : {durations[0]:.4f}")
        print(f"  Median : {durations[n // 2]:.4f}")
        print(f"  Mean   : {sum(durations) / n:.4f}")
        print(f"  p95    : {durations[int(n * 0.95)]:.4f}")
        print(f"  p99    : {durations[int(n * 0.99)]:.4f}")
        print(f"  Max    : {durations[-1]:.4f}")

        # Slow requests breakdown (>5s)
        slow = [d for d in durations if d > 5.0]
        if slow:
            print(f"  Slow (>5s) : {len(slow)} requests ({len(slow) / n * 100:.1f}%)")

    if sizes:
        sec("RESPONSE SIZE")
        print(f"  Total  : {fmt_bytes(sum(sizes))}")
        print(f"  Mean   : {fmt_bytes(sum(sizes) // len(sizes))}")
        print(f"  Max    : {fmt_bytes(max(sizes))}")

    print()


if __name__ == "__main__":
    main()
