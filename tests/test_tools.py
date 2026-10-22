import os

os.environ["DISABLE_LLM"] = "1"

from backend import tools
from backend.agent import run_stream


def test_onset_detects_last_episode():
    rows = [{"minute": f"m{i}", "error_rate": r, "total_requests": 100}
            for i, r in enumerate([0.01, 0.01, 0.02, 0.12, 0.15])]
    res = tools.summarize_error_rate(rows)
    assert res["onset_minute"] == "m3" and res["peak_minute"] == "m4"


def test_empty_rows():
    assert tools.summarize_error_rate([])["onset_minute"] is None


def test_spans_pick_most_errors():
    resp = {"aggregations": {"by_operation": {"buckets": [
        {"key": "a", "doc_count": 10, "errors": {"doc_count": 1}, "p99_ns": {"values": {"99.0": 3e9}}},
        {"key": "b", "doc_count": 9, "errors": {"doc_count": 8}, "p99_ns": {"values": {"99.0": 4e9}}}]}}}
    res = tools.summarize_spans(resp)
    assert res["faulty_operation"] == "b" and res["error_count"] == 8 and res["p99_ms"] == 4000


def test_spans_query_threshold():
    body = tools.slow_spans_body("svc", 2000)
    assert body["query"]["bool"]["must"][1]["range"]["durationInNanos"]["gte"] == 2_000_000_000


def test_impact_summary():
    assert tools.summarize_impact([("pending", 500, "t")])["pending_orders"] == 500


def test_demo_mode_end_to_end(monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "true")
    events = list(run_stream("Pourquoi le taux d'erreur a-t-il augmenté sur le service de paiement ?"))
    final = events[-1]
    assert final["type"] == "final"
    assert final["root_cause"]["faulty_component"] == "db.query.orders"
    assert final["metrics"]["accuracy"] == 1.0
    assert sum(e["type"] == "step_done" for e in events) == 5


def test_services_health_flags_incident_first():
    rows = [("auth", 50, 0, 100, 1), ("pay", 300, 140, 700, 7), ("tiny", 5, 5, 10, 0)]
    res = tools.summarize_services_health(rows)
    assert [r["service"] for r in res][0] == "pay" and res[0]["status"] == "incident"
    assert {r["service"]: r["status"] for r in res}["tiny"] == "healthy"  # trop peu de trafic


def test_score_services_finds_degraded_service():
    before = {("checkout", "hipstershop.EmailService/Send"): (100, 3.0, 0), ("checkout", "hipstershop.CartService/Get"): (100, 10.0, 0),
              ("email", "grpc.hipstershop.EmailService/Send"): (100, 0.3, 0), ("frontend", "frontend"): (100, 200.0, 0)}
    after = {("checkout", "hipstershop.EmailService/Send"): (100, 9.0, 0), ("checkout", "hipstershop.CartService/Get"): (100, 25.0, 0),
             ("email", "grpc.hipstershop.EmailService/Send"): (100, 0.3, 0), ("frontend", "frontend"): (100, 210.0, 0)}
    assert tools.score_services(before, after)[0]["service"] == "checkout"


def test_score_services_network_fault_blames_callee():
    # le callee n'est pas lent côté serveur, mais ses appels clients (émis par frontend) le sont
    before = {("frontend", "hipstershop.CurrencyService/Convert"): (100, 4.0, 0), ("currency", "grpc.hipstershop.CurrencyService/Convert"): (100, 0.1, 0),
              ("frontend", "hipstershop.CartService/Get"): (100, 5.0, 0), ("cart", "grpc.hipstershop.CartService/Get"): (100, 0.2, 0)}
    after = {("frontend", "hipstershop.CurrencyService/Convert"): (100, 40.0, 0), ("currency", "grpc.hipstershop.CurrencyService/Convert"): (100, 0.1, 0),
             ("frontend", "hipstershop.CartService/Get"): (100, 5.1, 0), ("cart", "grpc.hipstershop.CartService/Get"): (100, 0.2, 0)}
    assert tools.score_services(before, after)[0]["service"] == "currency"


def test_watchdog_opens_alert_once_and_resolves(monkeypatch):
    from backend import watchdog
    watchdog.reset()
    state = {"status": "incident"}
    monkeypatch.setattr(watchdog.tools, "services_health", lambda *a, **k: [
        {"service": "svc-a", "recent_error_rate": 0.4, "status": state["status"]}])
    calls = []
    monkeypatch.setattr(watchdog, "diagnose", lambda svc: calls.append(svc) or {
        "root_cause": {"summary": "s", "business_impact": "i"}, "metrics": {"diagnosis_seconds": 1}, "evidence": []})
    assert len(watchdog.tick()) == 1          # nouvel incident -> alerte
    assert watchdog.tick() == []              # déjà ouvert -> pas de doublon
    assert calls == ["svc-a"]
    state["status"] = "healthy"
    watchdog.tick()
    assert watchdog.alerts()[0]["resolved_at"] and watchdog.status()["open_incidents"] == []
