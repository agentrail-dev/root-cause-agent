"""Les 3 outils de l'agent. Chacun retourne un JSON structuré.

En DEMO_MODE=true, les résultats viennent de backend/fixtures/*.json (aucune base requise).
Les requêtes sont paramétrées (pas d'interpolation de chaînes utilisateur).
"""
import json
import os
from datetime import datetime
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures"


def demo_mode() -> bool:
    return os.getenv("DEMO_MODE", "false").lower() in ("1", "true", "yes")


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text())


def _iso(v) -> str | None:
    return v.isoformat(sep=" ", timespec="seconds") if isinstance(v, datetime) else (str(v) if v else None)


def clickhouse_client():
    import clickhouse_connect
    from urllib.parse import unquote, urlparse

    u = urlparse(os.environ["CLICKHOUSE_URL"])
    return clickhouse_connect.get_client(
        host=u.hostname, port=u.port or 8443, username=unquote(u.username or "default"),
        password=unquote(u.password or ""), secure=u.scheme == "https")


# ---------- Outil 1 : ClickHouse ----------
ERROR_RATE_SQL = """
SELECT toStartOfMinute(timestamp) AS minute,
       countIf(status_code >= 500) / count() AS error_rate,
       count() AS total_requests
FROM logs
WHERE service_name = {service:String}
  AND timestamp >= now() - INTERVAL {window:UInt32} MINUTE
GROUP BY minute
ORDER BY minute DESC
LIMIT 30
"""


def summarize_error_rate(rows: list[dict], spike_factor: float = 3.0, min_rate: float = 0.05) -> dict:
    """rows: [{minute, error_rate, total_requests}] (ordre quelconque).

    onset_minute = première minute (chronologique) du dernier épisode où le taux dépasse
    max(min_rate, spike_factor x baseline), baseline = médiane des taux.
    """
    rows = sorted(rows, key=lambda r: r["minute"])
    if not rows:
        return {"minutes": [], "peak_minute": None, "peak_error_rate": 0.0, "onset_minute": None}
    rates = sorted(r["error_rate"] for r in rows)
    baseline = rates[len(rates) // 2]
    threshold = max(min_rate, spike_factor * baseline)
    onset = None
    for r in rows:
        if r["error_rate"] >= threshold:
            if onset is None:
                onset = r["minute"]
        else:
            onset = None  # on veut le début du dernier épisode continu
    peak = max(rows, key=lambda r: r["error_rate"])
    return {
        "minutes": rows,
        "baseline_error_rate": round(baseline, 4),
        "peak_minute": peak["minute"],
        "peak_error_rate": round(peak["error_rate"], 4),
        "onset_minute": onset,
    }


def clickhouse_error_rate(service_name: str, time_window_minutes: int = 60) -> dict:
    if demo_mode():
        return _fixture("clickhouse_error_rate")
    client = clickhouse_client()
    res = client.query(ERROR_RATE_SQL, parameters={"service": service_name, "window": time_window_minutes})
    rows = [
        {"minute": _iso(m), "error_rate": float(e), "total_requests": int(t)}
        for m, e, t in res.result_rows
    ]
    return summarize_error_rate(rows)


# ---------- Outil 2 : OpenSearch ----------
def slow_spans_body(service_name: str, threshold_ms: int) -> dict:
    return {
        "size": 0,
        "query": {"bool": {"must": [
            {"term": {"serviceName": service_name}},
            {"range": {"durationInNanos": {"gte": threshold_ms * 1_000_000}}},
        ]}},
        "aggs": {"by_operation": {
            "terms": {"field": "operationName", "size": 10},
            "aggs": {
                "errors": {"filter": {"term": {"status.code": "ERROR"}}},
                "p99_ns": {"percentiles": {"field": "durationInNanos", "percents": [99]}},
            },
        }},
    }


def summarize_spans(response: dict) -> dict:
    buckets = response.get("aggregations", {}).get("by_operation", {}).get("buckets", [])
    ops = []
    for b in buckets:
        p99 = next(iter(b.get("p99_ns", {}).get("values", {}).values()), None)
        ops.append({
            "operation": b["key"],
            "slow_spans": b["doc_count"],
            "error_count": b["errors"]["doc_count"],
            "p99_ms": round(p99 / 1e6) if p99 else None,
        })
    ops.sort(key=lambda o: (o["error_count"], o["slow_spans"]), reverse=True)
    top = ops[0] if ops else None
    return {
        "operations": ops,
        "faulty_operation": top["operation"] if top else None,
        "error_count": top["error_count"] if top else 0,
        "p99_ms": top["p99_ms"] if top else None,
    }


def opensearch_slow_spans(service_name: str, threshold_ms: int = 2000) -> dict:
    if demo_mode():
        return _fixture("opensearch_slow_spans")
    from opensearchpy import OpenSearch

    client = OpenSearch(hosts=[os.environ["OPENSEARCH_URL"]])
    resp = client.search(index="otel-v1-apm-span-*", body=slow_spans_body(service_name, threshold_ms))
    return summarize_spans(resp)


# ---------- Outil 3 : PostgreSQL ----------
IMPACT_SQL = """
SELECT status, count(*) AS order_count, min(created_at) AS oldest_pending
FROM orders
WHERE status IN ('pending', 'processing')
  AND created_at >= now() - make_interval(mins => %s)
GROUP BY status
"""


def summarize_impact(rows: list[tuple]) -> dict:
    return {
        "pending_orders": sum(r[1] for r in rows),
        "by_status": {r[0]: r[1] for r in rows},
        "oldest_pending": min((_iso(r[2]) for r in rows), default=None),
    }


def postgres_business_impact(time_window_minutes: int = 120) -> dict:
    if demo_mode():
        return _fixture("postgres_business_impact")
    import psycopg

    with psycopg.connect(os.environ["POSTGRES_URL"]) as conn:
        rows = conn.execute(IMPACT_SQL, (time_window_minutes,)).fetchall()
    return summarize_impact(rows)


# ---------- Définitions de tools (format Anthropic) ----------
TOOL_DEFINITIONS = [
    {"name": "clickhouse_error_rate", "description": "Taux d'erreur 5xx par minute d'un service (ClickHouse).",
     "input_schema": {"type": "object", "properties": {"service_name": {"type": "string"},
                      "time_window_minutes": {"type": "integer", "default": 60}}, "required": ["service_name"]}},
    {"name": "opensearch_slow_spans", "description": "Spans lents/en erreur par opération (OpenSearch).",
     "input_schema": {"type": "object", "properties": {"service_name": {"type": "string"},
                      "threshold_ms": {"type": "integer", "default": 2000}}, "required": ["service_name"]}},
    {"name": "postgres_business_impact", "description": "Commandes pending/processing sur la fenêtre (PostgreSQL).",
     "input_schema": {"type": "object", "properties": {"time_window_minutes": {"type": "integer", "default": 120}}}},
]
