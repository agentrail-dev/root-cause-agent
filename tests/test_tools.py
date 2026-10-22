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
