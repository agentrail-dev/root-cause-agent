import json

from pathlib import Path

from fastapi import FastAPI
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


# Front statique, monté en dernier pour ne pas masquer les routes API.
app.mount("/", StaticFiles(directory=Path(__file__).parent.parent / "frontend" / "static", html=True), name="ui")
