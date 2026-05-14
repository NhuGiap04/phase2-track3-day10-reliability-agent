from __future__ import annotations

import copy
import json
import random
from pathlib import Path

from reliability_lab.cache import ResponseCache, SharedRedisCache
from reliability_lab.circuit_breaker import CircuitBreaker
from reliability_lab.config import LabConfig, ScenarioConfig
from reliability_lab.gateway import ReliabilityGateway
from reliability_lab.metrics import RunMetrics
from reliability_lab.providers import FakeLLMProvider


def load_queries(path: str | Path = "data/sample_queries.jsonl") -> list[str]:
    queries: list[str] = []
    for line in Path(path).read_text().splitlines():
        if not line.strip():
            continue
        queries.append(json.loads(line)["query"])
    return queries


def build_gateway(config: LabConfig, provider_overrides: dict[str, float] | None = None) -> ReliabilityGateway:
    providers = []
    for p in config.providers:
        fail_rate = provider_overrides.get(p.name, p.fail_rate) if provider_overrides else p.fail_rate
        providers.append(FakeLLMProvider(p.name, fail_rate, p.base_latency_ms, p.cost_per_1k_tokens))
    breakers = {
        p.name: CircuitBreaker(
            name=p.name,
            failure_threshold=config.circuit_breaker.failure_threshold,
            reset_timeout_seconds=config.circuit_breaker.reset_timeout_seconds,
            success_threshold=config.circuit_breaker.success_threshold,
        )
        for p in config.providers
    }
    cache: ResponseCache | SharedRedisCache | None = None
    if config.cache.enabled:
        if config.cache.backend == "redis":
            redis_cache = SharedRedisCache(
                config.cache.redis_url,
                config.cache.ttl_seconds,
                config.cache.similarity_threshold,
            )
            cache = redis_cache if redis_cache.ping() else ResponseCache(
                config.cache.ttl_seconds, config.cache.similarity_threshold
            )
        else:
            cache = ResponseCache(config.cache.ttl_seconds, config.cache.similarity_threshold)
    return ReliabilityGateway(providers, breakers, cache)


def calculate_recovery_time_ms(gateway: ReliabilityGateway) -> float | None:
    """Derive recovery time from circuit breaker transition logs.

    Recovery time = time between circuit opening and next successful close.
    Returns the average recovery time across all breakers, or None if no recovery occurred.
    """
    recovery_times: list[float] = []
    for breaker in gateway.breakers.values():
        open_ts: float | None = None
        for entry in breaker.transition_log:
            if entry["to"] == "open" and open_ts is None:
                open_ts = float(entry["ts"])
            elif entry["to"] == "closed" and open_ts is not None:
                recovery_times.append((float(entry["ts"]) - open_ts) * 1000)
                open_ts = None
    if not recovery_times:
        return None
    return sum(recovery_times) / len(recovery_times)


def run_scenario(config: LabConfig, queries: list[str], scenario: ScenarioConfig) -> RunMetrics:
    """Run a single named chaos scenario."""
    random.seed(_scenario_seed(scenario.name))
    scenario_config = copy.deepcopy(config)
    if scenario.name in {"primary_timeout_100", "primary_flaky_50"}:
        scenario_config.cache.enabled = False
    if scenario.name == "cache_stale_candidate":
        scenario_config.cache.similarity_threshold = 0.3

    gateway = build_gateway(scenario_config, scenario.provider_overrides or None)
    metrics = RunMetrics()
    request_count = scenario_config.load_test.requests
    for _ in range(request_count):
        prompt = random.choice(queries)
        result = gateway.complete(prompt)
        metrics.total_requests += 1
        metrics.estimated_cost += result.estimated_cost
        if result.cache_hit:
            metrics.cache_hits += 1
            metrics.estimated_cost_saved += 0.001
        if result.route.startswith("fallback:"):
            metrics.fallback_successes += 1
            metrics.successful_requests += 1
        elif result.route.startswith("static_fallback"):
            metrics.static_fallbacks += 1
            metrics.failed_requests += 1
        else:
            metrics.successful_requests += 1
        if result.latency_ms:
            metrics.latencies_ms.append(result.latency_ms)

    if scenario.name == "cache_stale_candidate" and gateway.cache is not None:
        gateway.cache.set("Summarize refund policy for 2024 deadline", "Old refund policy")
        stale_cached, stale_score = gateway.cache.get("Summarize refund policy for 2026 deadline")
        metrics.scenario_details[scenario.name] = {
            "false_hit_prevented": stale_cached is None,
            "false_hit_score": round(stale_score, 4),
            "false_hit_log_count": len(gateway.cache.false_hit_log),
        }

    metrics.circuit_open_count = sum(
        1 for breaker in gateway.breakers.values() for t in breaker.transition_log if t["to"] == "open"
    )
    metrics.recovery_time_ms = calculate_recovery_time_ms(gateway)
    return metrics


def run_simulation(config: LabConfig, queries: list[str]) -> RunMetrics:
    """Run all named scenarios from config, or a default run if none defined.

    TODO(student): Add a cache vs no-cache comparison scenario.
    Extend with your own custom scenarios (e.g., cost cap near limit).
    """
    random.seed(42)
    if not config.scenarios:
        default_scenario = ScenarioConfig(name="default", description="baseline run")
        metrics = run_scenario(config, queries, default_scenario)
        metrics.scenarios = {"default": "pass" if metrics.successful_requests > 0 else "fail"}
        metrics.cache_comparison = cache_comparison(config, queries)
        return metrics

    combined = RunMetrics()
    for scenario in config.scenarios:
        result = run_scenario(config, queries, scenario)

        passed = scenario_passed(scenario.name, result)
        combined.scenarios[scenario.name] = "pass" if passed else "fail"
        combined.scenario_details[scenario.name] = scenario_detail(scenario.name, result)

        combined.total_requests += result.total_requests
        combined.successful_requests += result.successful_requests
        combined.failed_requests += result.failed_requests
        combined.fallback_successes += result.fallback_successes
        combined.static_fallbacks += result.static_fallbacks
        combined.cache_hits += result.cache_hits
        combined.circuit_open_count += result.circuit_open_count
        combined.estimated_cost += result.estimated_cost
        combined.estimated_cost_saved += result.estimated_cost_saved
        combined.latencies_ms.extend(result.latencies_ms)
        if result.recovery_time_ms is not None:
            if combined.recovery_time_ms is None:
                combined.recovery_time_ms = result.recovery_time_ms
            else:
                combined.recovery_time_ms = (combined.recovery_time_ms + result.recovery_time_ms) / 2

    combined.cache_comparison = cache_comparison(config, queries)
    return combined


def _scenario_seed(name: str) -> int:
    return 1000 + sum(ord(char) for char in name)


def scenario_passed(name: str, metrics: RunMetrics) -> bool:
    if name == "primary_timeout_100":
        return (
            metrics.availability >= 0.95
            and metrics.static_fallbacks == 0
            and metrics.circuit_open_count > 0
        )
    if name == "primary_flaky_50":
        return metrics.availability >= 0.90 and metrics.circuit_open_count > 0
    if name == "cache_stale_candidate":
        detail = metrics.scenario_details.get(name, {})
        return bool(detail.get("false_hit_prevented")) and metrics.availability >= 0.95
    if name == "all_healthy":
        return metrics.availability >= 0.99 and metrics.static_fallbacks == 0
    return metrics.successful_requests > 0


def scenario_detail(name: str, metrics: RunMetrics) -> dict[str, object]:
    detail: dict[str, object] = {
        "availability": round(metrics.availability, 4),
        "fallback_success_rate": round(metrics.fallback_success_rate, 4),
        "cache_hit_rate": round(metrics.cache_hit_rate, 4),
        "circuit_open_count": metrics.circuit_open_count,
        "static_fallbacks": metrics.static_fallbacks,
        "recovery_time_ms": metrics.recovery_time_ms,
    }
    detail.update(metrics.scenario_details.get(name, {}))
    return detail


def cache_comparison(config: LabConfig, queries: list[str]) -> dict[str, dict[str, object]]:
    no_cache_config = copy.deepcopy(config)
    no_cache_config.cache.enabled = False
    with_cache_config = copy.deepcopy(config)
    with_cache_config.cache.enabled = True
    if with_cache_config.cache.backend == "redis":
        with_cache_config.cache.backend = "memory"

    baseline = run_scenario(no_cache_config, queries, ScenarioConfig(name="cache_compare_no_cache"))
    cached = run_scenario(with_cache_config, queries, ScenarioConfig(name="cache_compare_with_cache"))

    baseline_report = baseline.to_report_dict()
    cached_report = cached.to_report_dict()
    return {
        "without_cache": {
            "latency_p50_ms": baseline_report["latency_p50_ms"],
            "latency_p95_ms": baseline_report["latency_p95_ms"],
            "estimated_cost": baseline_report["estimated_cost"],
            "cache_hit_rate": baseline_report["cache_hit_rate"],
        },
        "with_cache": {
            "latency_p50_ms": cached_report["latency_p50_ms"],
            "latency_p95_ms": cached_report["latency_p95_ms"],
            "estimated_cost": cached_report["estimated_cost"],
            "cache_hit_rate": cached_report["cache_hit_rate"],
        },
    }
