import json

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import tools
from .agent import run_stream

app = FastAPI(title="Root Cause Agent")


class DiagnoseRequest(BaseModel):
    question: str = "Diagnostiquer l'incident en cours"
    service: str | None = None


@app.get("/health")
def health():
    return {"ok": True, "demo_mode": tools.demo_mode()}


@app.post("/diagnose")
def diagnose(req: DiagnoseRequest):
    """Flux NDJSON : un événement JSON par ligne (step_start / step_done / final / error)."""
    def gen():
        try:
            for ev in run_stream(req.question, req.service):
                yield json.dumps(ev, ensure_ascii=False, default=str) + "\n"
        except Exception as e:
            yield json.dumps({"type": "error", "message": str(e)}) + "\n"

    return StreamingResponse(gen(), media_type="application/x-ndjson")


@app.get("/incidents")
def incidents():
    """Santé de tous les services ; ceux en statut `incident` sont des incidents ouverts."""
    return {"services": tools.services_health()}


@app.get("/services/{name}")
def service_detail(name: str):
    """Série d'erreurs, opérations lentes et impact métier d'un service, sans passer par l'agent."""
    svc = next((s for s in tools.services_health() if s["service"] == name), None)
    if svc is None:
        raise HTTPException(404, "Service inconnu")
    incident = svc["status"] == "incident"
    if tools.demo_mode() and not incident:
        # Les fixtures décrivent uniquement l'incident : un service sain est une série plate au niveau normal.
        base = svc["baseline_error_rate"]
        minutes = [{"minute": f"2026-10-22 03:{m:02d}:00", "error_rate": round(max(base + d, 0), 4),
                    "total_requests": svc["recent_requests"] // 10}
                   for m, d in zip(range(5, 15), [0, .004, -.002, .003, 0, -.003, .002, 0, .003, -.002])]
        return {"service": svc, "error_rate": {"minutes": minutes, "baseline_error_rate": base,
                                               "onset_minute": None}, "slow_spans": {"operations": []},
                "business_impact": None}
    detail = {"service": svc, "error_rate": None, "slow_spans": None, "business_impact": None}
    for key, fn, args in (("error_rate", tools.clickhouse_error_rate, (name,)),
                          ("slow_spans", tools.opensearch_slow_spans, (name,)),
                          ("business_impact", tools.postgres_business_impact, ())):
        if key == "business_impact" and not incident:
            continue
        try:
            detail[key] = fn(*args)
        except Exception as e:  # une source indisponible ne doit pas casser la page
            detail[key] = {"error": str(e)}
    return detail


# Front statique, monté en dernier pour ne pas masquer les routes API.
app.mount("/", StaticFiles(directory=Path(__file__).parent.parent / "frontend" / "static", html=True), name="ui")
