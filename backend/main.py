import json

from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from . import tools
from .agent import run_stream

app = FastAPI(title="Root Cause Agent")


class DiagnoseRequest(BaseModel):
    question: str


@app.get("/health")
def health():
    return {"ok": True, "demo_mode": tools.demo_mode()}


@app.post("/diagnose")
def diagnose(req: DiagnoseRequest):
    """Flux NDJSON : un événement JSON par ligne (step_start / step_done / final / error)."""
    def gen():
        try:
            for ev in run_stream(req.question):
                yield json.dumps(ev, ensure_ascii=False, default=str) + "\n"
        except Exception as e:
            yield json.dumps({"type": "error", "message": str(e)}) + "\n"

    return StreamingResponse(gen(), media_type="application/x-ndjson")
