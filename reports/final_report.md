# Day 10 Reliability Final Report

## 1. Architecture summary

The gateway checks cache first, then routes through per-provider circuit breakers in configured order. If all providers fail or are open, it returns a static degraded-service response.

```text
User request
  -> Gateway
  -> Cache check (exact/similar, privacy and false-hit guards)
  -> Circuit breaker: primary -> Provider primary
  -> Circuit breaker: backup -> Provider backup
  -> Static fallback
```

## 2. Configuration

| Setting | Value | Reason |
|---|---:|---|
| failure_threshold | 3 | Opens after repeated failures without reacting to one-off jitter. |
| reset_timeout_seconds | 2 | Gives the provider a short recovery window before one probe. |
| success_threshold | 1 | One successful half-open probe is enough for this local fake provider lab. |
| cache TTL | 300 | Keeps FAQ-style answers reusable while limiting stale responses. |
| similarity_threshold | 0.92 | High threshold favors exact/repeated intents and reduces false hits. |
| cache backend | memory | Default local backend; Redis can be enabled with the same interface. |
| load_test requests | 100 | Enough requests to exercise cache reuse and circuit transitions. |

## 3. SLO definitions

| SLI | SLO target | Actual value | Met? |
|---|---|---:|---|
| Availability | >= 99% | 0.995 | yes |
| Latency P95 | < 2500 ms | 502.46 | yes |
| Fallback success rate | >= 95% | 0.9883 | yes |
| Cache hit rate | >= 10% | 0.36 | yes |
| Recovery time | < 5000 ms | 4042.103171348572 | yes |

## 4. Metrics

| Metric | Value |
|---|---:|
| total_requests | 400 |
| availability | 0.995 |
| error_rate | 0.005 |
| latency_p50_ms | 223.14 |
| latency_p95_ms | 502.46 |
| latency_p99_ms | 533.32 |
| fallback_success_rate | 0.9883 |
| cache_hit_rate | 0.36 |
| circuit_open_count | 22 |
| recovery_time_ms | 4042.103171348572 |
| estimated_cost | 0.10831 |
| estimated_cost_saved | 0.144 |

## 5. Cache comparison

| Metric | Without cache | With cache | Delta |
|---|---:|---:|---:|
| latency_p50_ms | 220.17 | 0.09 | -220.0800 (-100.0%) |
| latency_p95_ms | 508.29 | 474.52 | -33.7700 (-6.6%) |
| estimated_cost | 0.053792 | 0.009688 | -0.0441 (-82.0%) |
| cache_hit_rate | 0.0 | 0.81 | 0.8100 |

## 6. Redis shared cache

In-memory cache is local to one process, so horizontally scaled gateway instances would miss entries created elsewhere. `SharedRedisCache` stores responses under a shared Redis key prefix with TTLs, while applying the same privacy and false-hit guardrails as the in-memory cache.

Evidence: `tests/test_redis_cache.py::test_shared_state_across_instances` creates two cache clients with the same prefix and verifies the second reads data written by the first. Redis tests are skipped when Redis is not running; start it with `docker compose up -d`.

Redis CLI check after a Redis-backed run:

```bash
docker compose exec redis redis-cli KEYS "rl:cache:*"
```

Observed output from this run:

```text
rl:cache:e6bb724160ee
```

## 7. Chaos scenarios

| Scenario | Expected behavior | Observed behavior | Pass/Fail |
|---|---|---|---|
| primary_timeout_100 | Primary opens; backup serves traffic. | availability=1.0, fallback_success_rate=1.0, cache_hit_rate=0.0, circuit_open_count=14, static_fallbacks=0, recovery_time_ms=None | pass |
| primary_flaky_50 | Primary opens under repeated failures; fallback covers misses. | availability=0.98, fallback_success_rate=0.9718, cache_hit_rate=0.0, circuit_open_count=8, static_fallbacks=2, recovery_time_ms=4042.103171348572 | pass |
| all_healthy | Primary serves traffic with no static fallback. | availability=1.0, fallback_success_rate=0.0, cache_hit_rate=0.7, circuit_open_count=0, static_fallbacks=0, recovery_time_ms=None | pass |
| cache_stale_candidate | Similar queries with different years do not reuse stale cache. | availability=1.0, fallback_success_rate=0.0, cache_hit_rate=0.74, circuit_open_count=0, static_fallbacks=0, recovery_time_ms=None, false_hit_prevented=True, false_hit_score=0.75, false_hit_log_count=1 | pass |

## 8. Failure analysis

Circuit breaker state is process-local. In a multi-instance deployment, one instance can learn that a provider is unhealthy while another keeps sending traffic until its own breaker opens. Before production, move circuit state or health signals into a shared store and add jittered probe limits.

## 9. Next steps

1. Share circuit breaker state across instances with Redis counters and expirations.
2. Add provider-level budget routing so expensive providers are skipped near cost limits.
3. Export Prometheus counters for request totals, latency, cache hits, and circuit state.