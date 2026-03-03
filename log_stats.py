#!/usr/bin/env python3
"""Parse a Caddy JSON access log and print useful stats."""

import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_LOG = Path(__file__).resolve().parent / "access.log"


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


def print_section(title: str):
    print(f"\n{'=' * 60}")
    print(f"  {title}")
    print(f"{'=' * 60}")


def print_counter(counter: Counter, limit: int = 20):
    if not counter:
        print("  (none)")
        return
    max_label = max(len(str(k)) for k in counter)
    for key, count in counter.most_common(limit):
        pct = count / sum(counter.values()) * 100
        bar = "#" * max(1, int(pct / 2))
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

    # ── Collect fields ──────────────────────────────────────────
    uris = Counter()
    methods = Counter()
    statuses = Counter()
    ips = Counter()
    user_agents = Counter()
    platforms = Counter()
    browsers = Counter()
    hosts = Counter()
    protos = Counter()
    tls_versions = Counter()
    hours = Counter()
    durations = []
    sizes = []
    referers = Counter()

    for e in entries:
        req = e.get("request", {})
        headers = req.get("headers", {})

        uris[req.get("uri", "?")] += 1
        methods[req.get("method", "?")] += 1
        statuses[e.get("status", 0)] += 1
        ips[req.get("remote_ip", "?")] += 1
        hosts[req.get("host", "?")] += 1
        protos[req.get("proto", "?")] += 1

        tls = req.get("tls", {})
        if tls:
            ver = tls.get("version", 0)
            tls_label = {771: "TLS 1.2", 772: "TLS 1.3"}.get(ver, f"0x{ver:X}")
            tls_versions[tls_label] += 1

        ua_raw = (headers.get("User-Agent") or [""])[0]
        if ua_raw:
            # simple browser detection
            short = ua_raw
            if "Chrome" in ua_raw and "Safari" in ua_raw and "Edg" not in ua_raw:
                short = "Chrome"
            elif "Firefox" in ua_raw:
                short = "Firefox"
            elif "Safari" in ua_raw and "Chrome" not in ua_raw:
                short = "Safari"
            elif "Edg" in ua_raw:
                short = "Edge"
            else:
                short = ua_raw[:60]
            browsers[short] += 1

        plat = (headers.get("Sec-Ch-Ua-Platform") or [""])[0].strip('"')
        if plat:
            platforms[plat] += 1

        ref = (headers.get("Referer") or [""])[0]
        if ref:
            referers[ref] += 1

        ts = e.get("ts")
        if ts:
            hours[ts_to_dt(ts).strftime("%Y-%m-%d %H:00")] += 1

        dur = e.get("duration")
        if dur is not None:
            durations.append(dur)

        sz = e.get("size")
        if sz is not None:
            sizes.append(sz)

    # ── Time range ──────────────────────────────────────────────
    timestamps = [e["ts"] for e in entries if "ts" in e]
    first = ts_to_dt(min(timestamps))
    last = ts_to_dt(max(timestamps))

    # ── Print report ────────────────────────────────────────────
    print_section("OVERVIEW")
    print(f"  Log file      : {log_path}")
    print(f"  Total requests: {len(entries)}")
    print(
        f"  Time range    : {first:%Y-%m-%d %H:%M:%S UTC} → {last:%Y-%m-%d %H:%M:%S UTC}"
    )
    span = (last - first).total_seconds()
    if span > 0:
        print(f"  Span          : {span:.0f}s  ({span / 3600:.2f} hours)")
        print(f"  Req/sec       : {len(entries) / span:.2f}")

    print_section("STATUS CODES")
    print_counter(statuses)

    print_section("HTTP METHODS")
    print_counter(methods)

    print_section("TOP URIs")
    print_counter(uris)

    print_section("TOP IPs")
    print_counter(ips)

    print_section("HOSTS")
    print_counter(hosts)

    print_section("PROTOCOLS")
    print_counter(protos)

    print_section("TLS VERSIONS")
    print_counter(tls_versions)

    print_section("BROWSERS")
    print_counter(browsers)

    print_section("PLATFORMS")
    print_counter(platforms)

    print_section("REFERERS")
    print_counter(referers)

    print_section("REQUESTS BY HOUR")
    for hour in sorted(hours):
        print(f"  {hour}  {hours[hour]:>6}")

    if durations:
        durations.sort()
        print_section("RESPONSE TIME (seconds)")
        print(f"  Min    : {durations[0]:.6f}")
        print(f"  Max    : {durations[-1]:.6f}")
        print(f"  Mean   : {sum(durations) / len(durations):.6f}")
        p50 = durations[len(durations) // 2]
        p95 = durations[int(len(durations) * 0.95)]
        p99 = durations[int(len(durations) * 0.99)]
        print(f"  p50    : {p50:.6f}")
        print(f"  p95    : {p95:.6f}")
        print(f"  p99    : {p99:.6f}")

    if sizes:
        print_section("RESPONSE SIZE")
        print(f"  Total  : {fmt_bytes(sum(sizes))}")
        print(f"  Min    : {fmt_bytes(min(sizes))}")
        print(f"  Max    : {fmt_bytes(max(sizes))}")
        print(f"  Mean   : {fmt_bytes(sum(sizes) // len(sizes))}")

    print()


if __name__ == "__main__":
    main()
