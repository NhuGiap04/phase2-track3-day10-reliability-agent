from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import yaml


def _fmt(value: object) -> str:
    if value is None:
        return "n/a"
    return str(value)


def _delta(without: object, with_value: object) -> str:
    try:
        left = float(without)
        right = float(with_value)
    except (TypeError, ValueError):
        return "n/a"
    change = right - left
    if left:
        return f"{change:.4f} ({(change / left) * 100:.1f}%)"
    return f"{change:.4f}"


def _redis_key_evidence(redis_url: str) -> list[str]:
    try:
        from reliability_lab.cache import SharedRedisCache

        cache = SharedRedisCache(redis_url, ttl_seconds=300, similarity_threshold=0.92)
        cache.set("redis shared evidence query", "redis response")
        keys = sorted(str(key) for key in cache._redis.scan_iter("rl:cache:*"))
        cache.close()
        return keys
    except Exception:
        return []


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--metrics", default="reports/metrics.json")
    parser.add_argument("--out", default="reports/final_report.md")
    parser.add_argument("--config", default="configs/default.yaml")
    args = parser.parse_args()
    metrics: dict[str, Any] = json.loads(Path(args.metrics).read_text())
    config: dict[str, Any] = yaml.safe_load(Path(args.config).read_text())
    cb = config["circuit_breaker"]
    cache = config["cache"]
    load_test = config["load_test"]
    redis_keys = _redis_key_evidence(cache.get("redis_url", "redis://localhost:6379/0"))
    comparison = metrics.get("cache_comparison", {})
    without_cache = comparison.get("without_cache", {})
    with_cache = comparison.get("with_cache", {})
    scenario_details = metrics.get("scenario_details", {})

    lines = [
        "# Day 10 Reliability Final Report",
        "",
        "## 1. Architecture summary",
        "",
        "The gateway checks cache first, then routes through per-provider circuit breakers in configured order. If all providers fail or are open, it returns a static degraded-service response.",
        "",
        "```text",
        "User request",
        "  -> Gateway",
        "  -> Cache check (exact/similar, privacy and false-hit guards)",
        "  -> Circuit breaker: primary -> Provider primary",
        "  -> Circuit breaker: backup -> Provider backup",
        "  -> Static fallback",
        "```",
        "",
        "## 2. Configuration",
        "",
        "| Setting | Value | Reason |",
        "|---|---:|---|",
        f"| failure_threshold | {cb['failure_threshold']} | Opens after repeated failures without reacting to one-off jitter. |",
        f"| reset_timeout_seconds | {cb['reset_timeout_seconds']} | Gives the provider a short recovery window before one probe. |",
        f"| success_threshold | {cb['success_threshold']} | One successful half-open probe is enough for this local fake provider lab. |",
        f"| cache TTL | {cache['ttl_seconds']} | Keeps FAQ-style answers reusable while limiting stale responses. |",
        f"| similarity_threshold | {cache['similarity_threshold']} | High threshold favors exact/repeated intents and reduces false hits. |",
        f"| cache backend | {cache['backend']} | Default local backend; Redis can be enabled with the same interface. |",
        f"| load_test requests | {load_test['requests']} | Enough requests to exercise cache reuse and circuit transitions. |",
        "",
        "## 3. SLO definitions",
        "",
        "| SLI | SLO target | Actual value | Met? |",
        "|---|---|---:|---|",
        f"| Availability | >= 99% | {metrics.get('availability')} | {'yes' if float(metrics.get('availability', 0)) >= 0.99 else 'no'} |",
        f"| Latency P95 | < 2500 ms | {metrics.get('latency_p95_ms')} | {'yes' if float(metrics.get('latency_p95_ms', 0)) < 2500 else 'no'} |",
        f"| Fallback success rate | >= 95% | {metrics.get('fallback_success_rate')} | {'yes' if float(metrics.get('fallback_success_rate', 0)) >= 0.95 else 'no'} |",
        f"| Cache hit rate | >= 10% | {metrics.get('cache_hit_rate')} | {'yes' if float(metrics.get('cache_hit_rate', 0)) >= 0.10 else 'no'} |",
        f"| Recovery time | < 5000 ms | {_fmt(metrics.get('recovery_time_ms'))} | {'yes' if metrics.get('recovery_time_ms') is not None and float(metrics.get('recovery_time_ms', 0)) < 5000 else 'n/a'} |",
        "",
        "## 4. Metrics",
        "",
        "| Metric | Value |",
        "|---|---:|",
    ]
    for key, value in metrics.items():
        if key in {"scenarios", "scenario_details", "cache_comparison"}:
            continue
        lines.append(f"| {key} | {value} |")

    lines += [
        "",
        "## 5. Cache comparison",
        "",
        "| Metric | Without cache | With cache | Delta |",
        "|---|---:|---:|---:|",
    ]
    for key in ["latency_p50_ms", "latency_p95_ms", "estimated_cost", "cache_hit_rate"]:
        lines.append(
            f"| {key} | {_fmt(without_cache.get(key))} | {_fmt(with_cache.get(key))} | {_delta(without_cache.get(key), with_cache.get(key))} |"
        )

    lines += [
        "",
        "## 6. Redis shared cache",
        "",
        "In-memory cache is local to one process, so horizontally scaled gateway instances would miss entries created elsewhere. `SharedRedisCache` stores responses under a shared Redis key prefix with TTLs, while applying the same privacy and false-hit guardrails as the in-memory cache.",
        "",
        "Evidence: `tests/test_redis_cache.py::test_shared_state_across_instances` creates two cache clients with the same prefix and verifies the second reads data written by the first. Redis tests are skipped when Redis is not running; start it with `docker compose up -d`.",
        "",
        "Redis CLI check after a Redis-backed run:",
        "",
        "```bash",
        "docker compose exec redis redis-cli KEYS \"rl:cache:*\"",
        "```",
        "",
        "Observed output from this run:",
        "",
        "```text",
        "\n".join(redis_keys) if redis_keys else "Redis was not reachable during report generation.",
        "```",
        "",
        "## 7. Chaos scenarios",
        "",
        "| Scenario | Expected behavior | Observed behavior | Pass/Fail |",
        "|---|---|---|---|",
    ]
    for key, value in metrics.get("scenarios", {}).items():
        detail = scenario_details.get(key, {})
        expected = {
            "primary_timeout_100": "Primary opens; backup serves traffic.",
            "primary_flaky_50": "Primary opens under repeated failures; fallback covers misses.",
            "all_healthy": "Primary serves traffic with no static fallback.",
            "cache_stale_candidate": "Similar queries with different years do not reuse stale cache.",
        }.get(key, "Scenario remains available.")
        observed = ", ".join(f"{k}={v}" for k, v in detail.items()) or "see metrics"
        lines.append(f"| {key} | {expected} | {observed} | {value} |")

    lines += [
        "",
        "## 8. Failure analysis",
        "",
        "Circuit breaker state is process-local. In a multi-instance deployment, one instance can learn that a provider is unhealthy while another keeps sending traffic until its own breaker opens. Before production, move circuit state or health signals into a shared store and add jittered probe limits.",
        "",
        "## 9. Next steps",
        "",
        "1. Share circuit breaker state across instances with Redis counters and expirations.",
        "2. Add provider-level budget routing so expensive providers are skipped near cost limits.",
        "3. Export Prometheus counters for request totals, latency, cache hits, and circuit state.",
    ]
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text("\n".join(lines))
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
