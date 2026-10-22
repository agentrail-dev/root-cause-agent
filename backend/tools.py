"""Les 3 outils de l'agent. Chacun retourne un JSON structuré.

En DEMO_MODE=true, les résultats viennent de backend/fixtures/*.json (aucune base requise).
Les requêtes sont paramétrées (pas d'interpolation de chaînes utilisateur).
"""
import json
import os
from datetime import datetime, timedelta, timezone
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


# ---------- Détection d'incidents (vue d'ensemble) ----------
SERVICES_HEALTH_SQL = """
SELECT service_name,
       countIf(timestamp >= now() - INTERVAL 10 MINUTE) AS recent_n,
       countIf(timestamp >= now() - INTERVAL 10 MINUTE AND status_code >= 500) AS recent_err,
       countIf(timestamp < now() - INTERVAL 10 MINUTE) AS base_n,
       countIf(timestamp < now() - INTERVAL 10 MINUTE AND status_code >= 500) AS base_err
FROM logs
WHERE timestamp >= now() - INTERVAL {window:UInt32} MINUTE
GROUP BY service_name
ORDER BY service_name
"""


def summarize_services_health(rows: list[tuple], spike_factor: float = 3.0, min_rate: float = 0.05,
                              min_requests: int = 20) -> list[dict]:
    """rows: (service, recent_n, recent_err, base_n, base_err) -> santé + statut par service."""
    out = []
    for svc, rn, re_, bn, be in rows:
        recent = re_ / rn if rn else 0.0
        base = be / bn if bn else 0.0
        incident = rn >= min_requests and recent >= max(min_rate, spike_factor * base)
        out.append({"service": svc, "recent_error_rate": round(recent, 4), "baseline_error_rate": round(base, 4),
                    "recent_requests": int(rn), "status": "incident" if incident else "healthy"})
    return sorted(out, key=lambda r: (r["status"] != "incident", -r["recent_error_rate"]))


def services_health(window_minutes: int = 60) -> list[dict]:
    if demo_mode():
        return _fixture("services_health")
    if live_active():
        return live_health()
    res = clickhouse_client().query(SERVICES_HEALTH_SQL, parameters={"window": window_minutes})
    return summarize_services_health(res.result_rows)


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


# =====================================================================
# Mode « cas » (benchmark RCAEval) : cause racine multi-services par comparaison avant/après
# =====================================================================
import re
import statistics


SPAN_INDEX = "otel-v1-apm-span-rcaeval"


def _opensearch():
    from opensearchpy import OpenSearch

    return OpenSearch(hosts=[os.environ["OPENSEARCH_URL"]], timeout=60)


def _ms(dt: datetime) -> int:
    return int(dt.replace(tzinfo=timezone.utc).timestamp() * 1000)


def list_cases() -> list[dict]:
    if demo_mode():
        return _fixture("cases")
    rows = clickhouse_client().query(
        "SELECT case_id, root_cause_service, fault_type, inject_ts, start_ts, end_ts, n_logs, n_spans "
        "FROM rca_cases ORDER BY case_id").result_rows
    return [{"case_id": r[0], "root_cause_service": r[1], "fault_type": r[2], "inject_ts": _iso(r[3]),
             "start_ts": _iso(r[4]), "end_ts": _iso(r[5]), "n_logs": r[6], "n_spans": r[7]} for r in rows]


LIVE = "LIVE"
_live_cache = {"t": 0.0, "v": False}


def live_active() -> bool:
    """Vrai si le simulateur de trafic écrit en ce moment (logs LIVE des 3 dernières minutes). Mis en cache 10 s."""
    import time

    if demo_mode() or not os.getenv("CLICKHOUSE_URL"):
        return False
    if time.time() - _live_cache["t"] > 10:
        try:
            n = clickhouse_client().query(
                "SELECT count() FROM rca_logs WHERE case_id = 'LIVE' AND ts > now() - INTERVAL 3 MINUTE").result_rows[0][0]
            _live_cache.update(t=time.time(), v=n > 0)
        except Exception:
            _live_cache.update(t=time.time(), v=False)
    return _live_cache["v"]


def live_fault_truth() -> dict | None:
    """Dernière panne injectée dans le simulateur (vérité terrain live), ou None."""
    if not os.getenv("POSTGRES_URL"):
        return None
    try:
        import psycopg

        with psycopg.connect(os.environ["POSTGRES_URL"], connect_timeout=5) as conn:
            r = conn.execute("SELECT service, kind, since FROM live_faults ORDER BY id DESC LIMIT 1").fetchone()
        return {"service": r[0], "kind": r[1], "since": r[2].astimezone(timezone.utc).replace(tzinfo=None)} if r else None
    except Exception:
        return None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def live_health() -> list[dict]:
    """Santé des services en direct : le watchdog compare les 90 dernières secondes à tout ce qui précède les 3 dernières minutes."""
    b0, b1, a0, a1 = case_periods(LIVE)
    osc = _opensearch()
    ranked = score_services(_op_stats(osc, LIVE, b0, b1), _op_stats(osc, LIVE, a0, a1), min_spans=20, error_signal=True)
    ch = clickhouse_client()
    rows = {r[0]: r[1:] for r in ch.query(
        "SELECT service_name, count(), countIf(status_code >= 500) FROM rca_logs "
        "WHERE case_id = 'LIVE' AND ts > now() - INTERVAL 2 MINUTE GROUP BY service_name").result_rows}
    top = ranked[0] if ranked else None
    out = []
    for r in ranked:
        n, e = rows.get(r["service"], (0, 0))
        out.append({"service": r["service"], "recent_error_rate": round(e / n, 4) if n else 0.0, "baseline_error_rate": 0.0,
                    "recent_requests": int(n), "score": r["score"],
                    "status": "incident" if top and r["service"] == top["service"] and r["score"] >= 2.0 else "healthy"})
    return sorted(out, key=lambda r: (r["status"] != "incident", -r["score"]))


def case_info(case_id: str) -> dict:
    if case_id == LIVE:
        now, f = _utcnow(), live_fault_truth()
        return {"case_id": LIVE, "root_cause_service": f and f["service"], "fault_type": f and f["kind"],
                "inject_ts": f and _iso(f["since"]), "start_ts": _iso(now - timedelta(minutes=20)), "end_ts": _iso(now),
                "n_logs": 0, "n_spans": 0}
    c = next((c for c in list_cases() if c["case_id"] == case_id), None)
    if not c:
        raise KeyError(f"cas inconnu : {case_id}")
    return c


def case_periods(case_id: str):
    """Fenêtre du cas découpée en tiers : référence = 1er tiers, incident = dernier tiers."""
    if case_id == LIVE:
        now = _utcnow()
        return now - timedelta(minutes=20), now - timedelta(minutes=3), now - timedelta(seconds=90), now
    info = case_info(case_id)
    cs, ce = datetime.fromisoformat(info["start_ts"]), datetime.fromisoformat(info["end_ts"])
    third = (ce - cs) / 3
    return cs, cs + third, ce - third, ce


def normalize_service(name: str) -> str:
    return re.sub(r"service$", "", re.sub(r"[-_]", "", name.lower()))


def target_service(operation: str) -> str | None:
    m = re.search(r"\.(\w+?)Service/", operation)
    return m.group(1).lower() if m and "Health" not in operation else None


def score_services(before: dict, after: dict, min_spans: int = 30, degraded: float = 1.5,
                   error_signal: bool = False) -> list[dict]:
    """before/after : {(service, operation): (n, mean_ms, errors)} -> services classés par dégradation.

    Trois signaux, on garde le plus fort :
    - serveur   : opérations que X sert lui-même (cible == X) ralenties ;
    - émetteur  : au moins 2 cibles différentes ralenties dans les appels SORTANTS de X
                  (X est saturé : CPU, mémoire, disque...) ;
    - cible     : appels vers X ralentis vus d'émetteurs dont les autres appels sont sains
                  (panne réseau/latence côté X), ratio relatif au reste de l'émetteur.
    """
    rows = []  # (service, op, target, ratio, before_ms, after_ms, errors)
    for (svc, op), (n, mean, err) in after.items():
        b = before.get((svc, op))
        if not b or b[0] < min_spans or n < min_spans or b[1] <= 0 or "opentelemetry.proto" in op:
            continue  # l'export de télémétrie est du bruit, pas un appel métier
        rows.append((svc, op, target_service(op), mean / b[1], b[1], mean, err))

    services = {r[0] for r in rows}
    out_by = {}  # émetteur -> {cible: [ratios]}
    in_by = {}   # cible -> [(émetteur, ratio)]
    served = {}  # service -> [ratios]
    for svc, op, tgt, ratio, *_ in rows:
        if not tgt:
            continue
        if normalize_service(svc) == tgt:
            served.setdefault(svc, []).append(ratio)
        else:
            out_by.setdefault(svc, {}).setdefault(tgt, []).append(ratio)
            in_by.setdefault(tgt, []).append((svc, ratio, op))

    out = []
    for svc in services:
        srv = statistics.median(served[svc]) if svc in served else 0.0
        outbound = {t: statistics.median(v) for t, v in out_by.get(svc, {}).items()}
        deg = [v for v in outbound.values() if v >= degraded]
        emitter = statistics.median(deg) if len(deg) >= 2 else 0.0
        rel = []
        for emitter_svc, ratio, _op in in_by.get(normalize_service(svc), []):
            others = [statistics.median(v) for t, v in out_by.get(emitter_svc, {}).items() if t != normalize_service(svc)]
            base = statistics.median(others) if others else 1.0
            if base < 1.3:  # émetteur lui-même sain : la lenteur est bien celle de la cible
                rel.append(ratio / max(1.0, base))
        target = statistics.median(rel) if rel else 0.0
        err_sig = 0.0
        if error_signal:  # taux d'erreur des opérations servies par X (après), utile pour les pannes « erreurs »
            sn = sum(after[(svc, r[1])][0] for r in rows if r[0] == svc and r[2] and normalize_service(svc) == r[2])
            se = sum(after[(svc, r[1])][2] for r in rows if r[0] == svc and r[2] and normalize_service(svc) == r[2])
            err_sig = 1 + 10 * se / sn if sn and se / sn >= 0.1 else 0.0
        mine = [r for r in rows if r[0] == svc or (r[2] and normalize_service(svc) == r[2])]
        top = sorted(mine, key=lambda r: r[3], reverse=True)[:3]
        out.append({"service": svc, "score": round(max(srv, emitter, target, err_sig), 2),
                    "signals": {"served": round(srv, 2), "outbound": round(emitter, 2), "inbound": round(target, 2),
                                "errors": round(err_sig, 2)},
                    "errors_after": sum(r[6] for r in rows if r[0] == svc),
                    "top_operations": [{"operation": r[1], "emitter": r[0], "before_ms": round(r[4], 1),
                                            "after_ms": round(r[5], 1)} for r in top]})
    return sorted(out, key=lambda r: r["score"], reverse=True)


def _op_stats(os_client, case_id: str, a: datetime, b: datetime) -> dict:
    q = {"size": 0, "query": {"bool": {"filter": [
        {"term": {"caseId": case_id}}, {"range": {"startTime": {"gte": _ms(a), "lt": _ms(b)}}}]}},
        "aggs": {"op": {"composite": {"size": 1000, "sources": [
            {"svc": {"terms": {"field": "serviceName"}}}, {"op": {"terms": {"field": "operationName"}}}]},
            "aggs": {"avg": {"avg": {"field": "durationInNanos"}},
                     "err": {"filter": {"term": {"status.code": "ERROR"}}}}}}}
    res = os_client.search(index=SPAN_INDEX, body=q)
    return {(k["key"]["svc"], k["key"]["op"]): (k["doc_count"], (k["avg"]["value"] or 0) / 1e6, k["err"]["doc_count"])
            for k in res["aggregations"]["op"]["buckets"]}


def logs_scan(case_id: str) -> dict:
    """ClickHouse : volume et logs d'erreur par service, avant/après ; série par minute."""
    if demo_mode():
        return _fixture("case_logs_scan")
    b0, b1, a0, a1 = case_periods(case_id)
    ch = clickhouse_client()
    rows = ch.query(
        "SELECT service_name, countIf(ts >= {b0:DateTime64(3)} AND ts < {b1:DateTime64(3)}) AS nb, "
        "countIf(ts >= {a0:DateTime64(3)} AND ts < {a1:DateTime64(3)}) AS na, "
        "countIf(status_code >= 500 AND ts >= {b0:DateTime64(3)} AND ts < {b1:DateTime64(3)}) AS eb, "
        "countIf(status_code >= 500 AND ts >= {a0:DateTime64(3)} AND ts < {a1:DateTime64(3)}) AS ea "
        "FROM rca_logs WHERE case_id = {c:String} GROUP BY service_name",
        parameters={"c": case_id, "b0": b0, "b1": b1, "a0": a0, "a1": a1}).result_rows
    services = sorted(({"service": s, "logs_before": nb, "logs_after": na, "errors_before": eb, "errors_after": ea,
                        "volume_ratio": round(na / nb, 2) if nb else None} for s, nb, na, eb, ea in rows),
                      key=lambda r: (r["errors_after"] - r["errors_before"], abs((r["volume_ratio"] or 1) - 1)), reverse=True)
    series = ch.query(
        "SELECT toStartOfMinute(ts) AS m, countIf(status_code >= 500) / count() AS error_rate, count() AS total_requests "
        "FROM rca_logs WHERE case_id = {c:String} GROUP BY m ORDER BY m", parameters={"c": case_id}).result_rows
    minutes = [{"minute": _iso(m), "error_rate": float(e), "total_requests": int(t)} for m, e, t in series]
    err_total = sum(r["errors_after"] for r in services)
    return {"minutes": minutes, "services": services[:8], "error_logs_after": err_total,
            "peak_minute": max(minutes, key=lambda m: m["total_requests"])["minute"] if minutes else None,
            "peak_error_rate": max((m["error_rate"] for m in minutes), default=0.0),
            "onset_minute": None,
            "note": ("Aucun log d'erreur : le signal est dans la latence, on poursuit avec les traces."
                     if err_total == 0 else f"{err_total} logs d'erreur dans la période d'incident.")}


def case_root_cause_spans(case_id: str) -> dict:
    """OpenSearch : service le plus dégradé (avant/après) + opération + début estimé."""
    if demo_mode():
        return _fixture("case_spans")
    b0, b1, a0, a1 = case_periods(case_id)
    osc = _opensearch()
    ranked = score_services(_op_stats(osc, case_id, b0, b1), _op_stats(osc, case_id, a0, a1),
                            min_spans=20 if case_id == LIVE else 30, error_signal=case_id == LIVE)
    if not ranked:
        return {"operations": [], "faulty_operation": None, "faulty_service": None, "error_count": 0, "p99_ms": None}
    top = ranked[0]
    svc = top["service"]
    top_op = top["top_operations"][0]  # série du signal le plus net, pas la moyenne de tout le service
    hist = osc.search(index=SPAN_INDEX, body={"size": 0, "query": {"bool": {"filter": [
        {"term": {"caseId": case_id}}, {"term": {"serviceName": top_op["emitter"]}},
        {"term": {"operationName": top_op["operation"]}}]}},
        "aggs": {"m": {"date_histogram": {"field": "startTime", "fixed_interval": "1m"},
                       "aggs": {"avg": {"avg": {"field": "durationInNanos"}}}}}})["aggregations"]["m"]["buckets"]
    series = [(datetime.fromtimestamp(b["key"] / 1000, timezone.utc).replace(tzinfo=None), (b["avg"]["value"] or 0) / 1e6)
              for b in hist if b["doc_count"] > 0]
    ref = [v for t, v in series if t < b1]
    onset = None
    if len(ref) >= 3:
        mu, sd = statistics.mean(ref), statistics.pstdev(ref)
        thr = max(mu + 3 * sd, mu * 1.3)
        for i, (t, v) in enumerate(series[:-1]):
            if t >= b1 - timedelta(minutes=1) and v > thr and series[i + 1][1] > thr:
                onset = _iso(t)
                break
    return {"operations": ranked[:8], "faulty_service": svc, "faulty_operation": top["top_operations"][0]["operation"],
            "score": top["score"], "error_count": top["errors_after"], "p99_ms": None, "onset_minute": onset,
            "latency_series": [{"minute": _iso(t), "avg_ms": round(v, 2)} for t, v in series]}


def case_business_impact(case_id: str) -> dict:
    """Impact métier tiré des vraies traces : appels de commande (PlaceOrder/Checkout) ralentis ou en erreur."""
    if demo_mode():
        return _fixture("case_impact")
    b0, b1, a0, a1 = case_periods(case_id)
    osc = _opensearch()

    def stats(a, b):
        r = osc.search(index=SPAN_INDEX, body={"size": 0, "query": {"bool": {"filter": [
            {"term": {"caseId": case_id}}, {"wildcard": {"operationName": "*CheckoutService/PlaceOrder"}},
            {"term": {"serviceName": "frontendservice"}}, {"range": {"startTime": {"gte": _ms(a), "lt": _ms(b)}}}]}},
            "aggs": {"avg": {"avg": {"field": "durationInNanos"}}, "p95": {"percentiles": {"field": "durationInNanos", "percents": [95]}},
                     "err": {"filter": {"term": {"status.code": "ERROR"}}}}})
        return (r["hits"]["total"]["value"], (r["aggregations"]["avg"]["value"] or 0) / 1e6,
                next(iter(r["aggregations"]["p95"]["values"].values())) / 1e6, r["aggregations"]["err"]["doc_count"])

    nb, mb, p95b, _ = stats(b0, b1)
    na, ma, _, ea = stats(a0, a1)
    slow = osc.count(index=SPAN_INDEX, body={"query": {"bool": {"filter": [
        {"term": {"caseId": case_id}}, {"wildcard": {"operationName": "*CheckoutService/PlaceOrder"}},
        {"term": {"serviceName": "frontendservice"}}, {"range": {"startTime": {"gte": _ms(a0), "lt": _ms(a1)}}},
        {"range": {"durationInNanos": {"gt": int(p95b * 1e6)}}}]}}})["count"]
    return {"pending_orders": slow + ea, "orders_in_incident_window": na, "slow_orders": slow, "failed_orders": ea,
            "mean_ms_before": round(mb, 1), "mean_ms_after": round(ma, 1), "oldest_pending": None,
            "source": "traces (appels PlaceOrder du frontend)"}
