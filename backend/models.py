"""Schémas Pydantic (sorties LLM contraintes) et état partagé du graphe."""
import operator
from typing import Annotated, Any, TypedDict

from pydantic import BaseModel, Field


class QuestionAnalysis(BaseModel):
    service_name: str = Field(description="Nom technique du service (ex: payment-processor)")
    time_window_minutes: int = Field(default=60, ge=5, le=1440)


class RootCause(BaseModel):
    incident_start: str = Field(description="Timestamp exact du début de l'incident")
    faulty_component: str = Field(description="Composant / opération fautif")
    evidence: str = Field(description="Preuve chiffrée issue des requêtes")
    business_impact: str = Field(description="Impact métier chiffré")
    summary: str = Field(description="Phrase de cause racine, en français")


class TimelineEvent(BaseModel):
    step: str
    status: str  # running | done
    message: str
    data: dict[str, Any] | None = None


class AgentState(TypedDict, total=False):
    question: str
    service_name: str
    time_window_minutes: int
    error_rate: dict
    slow_spans: dict
    business_impact: dict
    root_cause: dict
    evidence_count: int
    timeline: Annotated[list[dict], operator.add]
