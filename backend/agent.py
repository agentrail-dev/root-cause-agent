"""Orchestrateur LangGraph : analyze_question -> detect_anomaly -> find_faulty_span
-> check_business_impact -> synthesize.

Les appels LLM (extraction de la question, synthèse) sont contraints par un schéma
Pydantic via tool-use forcé. Sans ANTHROPIC_API_KEY, des heuristiques déterministes
prennent le relais, de sorte que la démo ne dépend jamais du réseau LLM.
"""
import contextvars
import json
import os
import re
import time
from datetime import datetime
from typing import Iterator

from langgraph.graph import END, START, StateGraph

from . import tools
from .models import AgentState, QuestionAnalysis, RootCause
from .prompts import ANALYZE_PROMPT, KNOWN_SERVICES, SYNTHESIZE_PROMPT

STEPS = ["analyze_question", "detect_anomaly", "find_faulty_span", "check_business_impact", "synthesize"]
STEP_LABELS = {
    "analyze_question": "Contexte : service et fenêtre",
    "detect_anomaly": "Quand : logs et signaux (ClickHouse)",
    "find_faulty_span": "Où : service le plus dégradé (OpenSearch)",
    "check_business_impact": "Impact : métier",
    "synthesize": "Conclusion causale",
}


LLM_ON = contextvars.ContextVar("llm_on", default=True)  # désactivé par appel (ex. benchmark de masse)


def llm_structured(system: str, user: str, schema):
    """Appel Claude avec sortie forcée au schéma Pydantic. None si indisponible/échec."""
    if not LLM_ON.get() or not os.getenv("ANTHROPIC_API_KEY") or os.getenv("DISABLE_LLM", "").lower() in ("1", "true"):
        return None
    try:
        import anthropic

        client = anthropic.Anthropic(timeout=30)
        user_msg, last_err = user, None
        for _ in range(2):  # 2e essai : on renvoie l'erreur de validation (le modèle oublie parfois un champ)
            resp = client.messages.create(
                model=os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5-5"),
                max_tokens=1500,
                system=system + "\n\nRéponds UNIQUEMENT en appelant l'outil `answer`, avec TOUS les champs requis.",
                messages=[{"role": "user", "content": user_msg}],
                tools=[{"name": "answer", "description": "Réponse structurée",
                        "input_schema": schema.model_json_schema()}],
                tool_choice={"type": "auto"},
            )
            block = next((b for b in resp.content if b.type == "tool_use"), None)
            if block is None:
                user_msg = user + "\n\nTu dois répondre en appelant l'outil `answer`."
                continue
            try:
                return schema.model_validate(block.input)
            except Exception as e:  # champ manquant : on réessaie en le signalant
                last_err = e
                user_msg = user + f"\n\nTa réponse précédente était invalide ({str(e)[:300]}). Renvoie TOUS les champs requis."
        print(f"[llm] fallback heuristique: {last_err}")
        return None
    except Exception as e:  # réseau, quota : on retombe sur l'heuristique
        print(f"[llm] fallback heuristique: {e}")
        return None


def _event(step: str, message: str, data: dict | None = None) -> dict:
    return {"step": step, "status": "done", "message": message, "data": data}


def _fmt_ts(ts: str | None) -> str:
    return ts.split(" ")[-1] if ts else "n/a"


# ---------- Nœuds ----------
def analyze_question(state: AgentState) -> dict:
    if state.get("case_id"):  # cas de benchmark : service d'alerte = frontend, fenêtre = celle du cas
        info = tools.case_info(state["case_id"])
        return {"service_name": "frontend", "time_window_minutes": 24,
                "timeline": [_event("analyze_question",
                                    f"Cas {state['case_id']} : télémétrie du {info['start_ts']} au {info['end_ts']} (UTC)",
                                    {"case_id": state["case_id"], "n_logs": info["n_logs"], "n_spans": info["n_spans"]})]}
    if state.get("service_name"):  # incident sélectionné dans la console : pas besoin d'extraire
        svc, win = state["service_name"], state.get("time_window_minutes") or 60
        return {"service_name": svc, "time_window_minutes": win,
                "timeline": [_event("analyze_question", f"Service cible : {svc}, fenêtre : {win} min",
                                    {"service_name": svc, "time_window_minutes": win})]}
    q = state["question"]
    services = ", ".join(KNOWN_SERVICES)
    out = llm_structured(ANALYZE_PROMPT.format(services=services), q, QuestionAnalysis)
    if out is None:
        low = q.lower()
        service = next((s for s, kws in KNOWN_SERVICES.items() if s in low or any(k in low for k in kws)),
                       next(iter(KNOWN_SERVICES)))
        m = re.search(r"(\d+)\s*(min|h)", low)
        window = int(m.group(1)) * (60 if m.group(2) == "h" else 1) if m else 60
        out = QuestionAnalysis(service_name=service, time_window_minutes=window)
    return {
        "service_name": out.service_name,
        "time_window_minutes": out.time_window_minutes,
        "timeline": [_event("analyze_question",
                            f"Service cible : {out.service_name}, fenêtre : {out.time_window_minutes} min",
                            out.model_dump())],
    }


def detect_anomaly(state: AgentState) -> dict:
    if state.get("case_id"):
        res = tools.logs_scan(state["case_id"])
        return {"error_rate": res, "timeline": [_event("detect_anomaly", res["note"], res)]}
    res = tools.clickhouse_error_rate(state["service_name"], state["time_window_minutes"])
    if not res.get("minutes"):
        msg = "Aucune donnée de logs sur la fenêtre."
    else:
        msg = (f"Début de la dégradation à {_fmt_ts(res.get('onset_minute'))}, "
               f"pic à {_fmt_ts(res['peak_minute'])} ({res['peak_error_rate']:.0%} d'erreurs 5xx).")
    return {"error_rate": res, "timeline": [_event("detect_anomaly", msg, res)]}


def find_faulty_span(state: AgentState) -> dict:
    if state.get("case_id"):
        res = tools.case_root_cause_spans(state["case_id"])
        if not res.get("faulty_service"):
            msg = "Aucun service dégradé trouvé dans les traces."
        else:
            msg = (f"Service le plus dégradé : {res['faulty_service']} (latence x{res['score']} vs référence), "
                   f"opération {res['faulty_operation']}"
                   + (f" ; début estimé {res['onset_minute']}." if res.get("onset_minute") else "."))
        return {"slow_spans": res, "timeline": [_event("find_faulty_span", msg, res)]}
    res = tools.opensearch_slow_spans(state["service_name"])
    if not res.get("faulty_operation"):
        msg = "Aucun span lent trouvé."
    else:
        msg = (f"L'opération {res['faulty_operation']} concentre {res['error_count']} erreurs"
               + (f" (p99 {res['p99_ms']} ms)." if res.get("p99_ms") else "."))
    return {"slow_spans": res, "timeline": [_event("find_faulty_span", msg, res)]}


def check_business_impact(state: AgentState) -> dict:
    if state.get("case_id"):
        res = tools.case_business_impact(state["case_id"])
        msg = (f"{res['pending_orders']} appels de commande ralentis ou en erreur sur {res['orders_in_incident_window']} "
               f"(latence moyenne {res['mean_ms_before']} → {res['mean_ms_after']} ms).")
        return {"business_impact": res, "timeline": [_event("check_business_impact", msg, res)]}
    res = tools.postgres_business_impact(max(state["time_window_minutes"], 120))
    n = res["pending_orders"]
    msg = f"Impact métier confirmé : {n} commandes bloquées." if n else "Aucun impact métier détecté."
    return {"business_impact": res, "timeline": [_event("check_business_impact", msg, res)]}


def _evidence(state: AgentState) -> list[str]:
    er, sp, bi = state["error_rate"], state["slow_spans"], state["business_impact"]
    ev = []
    if state.get("case_id"):
        if sp.get("faulty_service"):
            ev.append(f"{sp['faulty_service']} : latence x{sp['score']} entre la période de référence et la période d'incident")
            for o in sp["operations"][0]["top_operations"]:
                ev.append(f"{o['operation']} : {o['before_ms']} → {o['after_ms']} ms")
        if sp.get("onset_minute"):
            ev.append(f"Début de la dégradation estimé à {sp['onset_minute']}")
        ev.append(er["note"])
        if bi.get("pending_orders"):
            ev.append(f"{bi['pending_orders']} appels de commande impactés (traces)")
        return ev
    if er.get("onset_minute"):
        ev.append(f"Début de la hausse d'erreurs à {er['onset_minute']}")
    if er.get("peak_error_rate"):
        ev.append(f"Pic de {er['peak_error_rate']:.0%} d'erreurs 5xx à {er['peak_minute']}")
    if sp.get("faulty_operation"):
        ev.append(f"{sp['error_count']} spans en erreur sur {sp['faulty_operation']}")
    if sp.get("p99_ms"):
        ev.append(f"Latence p99 de {sp['p99_ms']} ms sur {sp['faulty_operation']}")
    if bi.get("pending_orders"):
        ev.append(f"{bi['pending_orders']} commandes pending")
    return ev


def synthesize(state: AgentState) -> dict:
    er, sp, bi = state["error_rate"], state["slow_spans"], state["business_impact"]
    case = bool(state.get("case_id"))
    start = (sp.get("onset_minute") if case else None) or er.get("onset_minute") or er.get("peak_minute") or "inconnu"
    comp = (sp.get("faulty_service") if case else sp.get("faulty_operation")) or "inconnu"
    ev = _evidence(state)
    payload = json.dumps({"question": state["question"], "service": state["service_name"], "case": state.get("case_id"),
                          "clickhouse": {k: v for k, v in er.items() if k != "minutes"},
                          "opensearch": sp, "postgres": bi}, ensure_ascii=False)
    rc = llm_structured(SYNTHESIZE_PROMPT, payload, RootCause)
    if rc is None and case:
        top = sp["operations"][0]["top_operations"][0] if sp.get("operations") else None
        rc = RootCause(
            incident_start=start, faulty_component=comp,
            evidence=(f"{top['operation']} passe de {top['before_ms']} à {top['after_ms']} ms; " if top else "")
                     + f"score de dégradation x{sp.get('score')}",
            business_impact=f"{bi['pending_orders']} appels de commande impactés (latence moyenne {bi['mean_ms_before']} → {bi['mean_ms_after']} ms)",
            summary=(f"Le service {comp} est la source de la dégradation (latence x{sp.get('score')} à partir de {start}) ; "
                     f"{bi['pending_orders']} appels de commande en sont affectés."),
            next_steps=[f"Inspecter les ressources (CPU, mémoire, disque, réseau) du service {comp}",
                        f"Vérifier les déploiements et changements récents sur {comp}",
                        f"Ajouter une alerte sur la latence de {comp}"])
    if rc is None:
        p99 = f" (p99 {sp['p99_ms']} ms)" if sp.get("p99_ms") else ""
        rc = RootCause(
            incident_start=start,
            faulty_component=comp,
            evidence=f"{sp.get('error_count', 0)} spans en erreur sur {comp}{p99}",
            business_impact=f"{bi['pending_orders']} commandes client bloquées en statut pending",
            next_steps=[f"Examiner le plan d'exécution et les verrous de {comp}",
                        f"Débloquer ou rejouer les {bi['pending_orders']} commandes pending une fois corrigé",
                        f"Ajouter une alerte sur le p99 de {comp}"],
            summary=(f"À {start}, l'opération {comp} a subi une dégradation{p99} : "
                     f"{sp.get('error_count', 0)} spans en erreur, {bi['pending_orders']} commandes bloquées."),
        )
    if not [x for x in rc.next_steps if x and x.strip()]:  # Claude omet parfois ce champ : actions de repli, jamais vide
        rc.next_steps = [f"Inspecter les ressources (CPU, mémoire, réseau) et les dépendances de {comp}",
                         f"Vérifier les déploiements et changements de configuration récents sur {comp} autour de {start}",
                         f"Ajouter une alerte sur la latence et le taux d'erreur de {comp}"]
    if case and sp.get("faulty_service"):  # le composant vient de la donnée, pas de la prose du LLM
        rc.faulty_component = sp["faulty_service"]
    elif sp.get("faulty_operation"):
        rc.faulty_component = sp["faulty_operation"]
    return {
        "root_cause": rc.model_dump(),
        "evidence_count": len(ev),
        "timeline": [_event("synthesize", rc.summary, {"evidence": ev})],
    }


def build_graph():
    g = StateGraph(AgentState)
    fns = [analyze_question, detect_anomaly, find_faulty_span, check_business_impact, synthesize]
    for f in fns:
        g.add_node(f.__name__, f)
    g.add_edge(START, STEPS[0])
    for a, b in zip(STEPS, STEPS[1:]):
        g.add_edge(a, b)
    g.add_edge(STEPS[-1], END)
    return g.compile()


GRAPH = build_graph()
HUMAN_BASELINE_SECONDS = 45 * 60


def ground_truth_operation() -> str | None:
    if os.getenv("GROUND_TRUTH_OPERATION"):
        return os.environ["GROUND_TRUTH_OPERATION"]
    if tools.demo_mode():
        return tools._fixture("ground_truth")["operation"]
    return None


def run_stream(question: str, service: str | None = None, case_id: str | None = None,
               use_llm: bool = True) -> Iterator[dict]:
    """Génère des événements : step_start, step_done, final (avec métriques)."""
    LLM_ON.set(use_llm)
    t0 = time.perf_counter()
    state: dict = {"question": question, "timeline": []}
    yield {"type": "step_start", "step": STEPS[0], "label": STEP_LABELS[STEPS[0]]}
    for update in GRAPH.stream({"question": question, "timeline": [], **({"service_name": service} if service else {}),
                              **({"case_id": case_id} if case_id else {})},
                              stream_mode="updates"):
        for node, delta in update.items():
            state.update({k: v for k, v in delta.items() if k != "timeline"})
            for ev in delta.get("timeline", []):
                yield {"type": "step_done", "label": STEP_LABELS[node], **ev}
            i = STEPS.index(node)
            if i + 1 < len(STEPS):
                yield {"type": "step_start", "step": STEPS[i + 1], "label": STEP_LABELS[STEPS[i + 1]]}
    elapsed = time.perf_counter() - t0
    found = state["root_cause"]["faulty_component"]
    if case_id:
        info = tools.case_info(case_id)
        onset = (state["slow_spans"] or {}).get("onset_minute")
        onset_err = (abs((datetime.fromisoformat(onset) - datetime.fromisoformat(info["inject_ts"])).total_seconds())
                     if onset and info.get("inject_ts") else None)
        elapsed = time.perf_counter() - t0
        yield {"type": "final", "root_cause": state["root_cause"], "service": found, "case_id": case_id,
               "evidence": _evidence(state),
               "metrics": {"diagnosis_seconds": round(elapsed, 1), "human_baseline_seconds": HUMAN_BASELINE_SECONDS,
                           "mttr_reduction": round(1 - elapsed / HUMAN_BASELINE_SECONDS, 4),
                           "evidence_count": state["evidence_count"],
                           "accuracy": (None if not info.get("root_cause_service")
                                        else 1.0 if found == info["root_cause_service"] else 0.0),
                           "ground_truth": (f"{info['root_cause_service']} ({info['fault_type']})"
                                            if info.get("root_cause_service") else None),
                           "onset_error_seconds": onset_err},
               "truth": info,
               "data": {"error_rate": state["error_rate"], "slow_spans": state["slow_spans"],
                        "business_impact": state["business_impact"]}}
        return
    truth = ground_truth_operation()
    yield {"type": "final", "root_cause": state["root_cause"], "metrics": {
        "diagnosis_seconds": round(elapsed, 1),
        "human_baseline_seconds": HUMAN_BASELINE_SECONDS,
        "mttr_reduction": round(1 - elapsed / HUMAN_BASELINE_SECONDS, 4),
        "evidence_count": state["evidence_count"],
        "accuracy": None if truth is None else (1.0 if found == truth else 0.0),
        "ground_truth": truth,
    }, "service": state["service_name"], "evidence": _evidence(state),
        "data": {"error_rate": state["error_rate"], "slow_spans": state["slow_spans"],
                 "business_impact": state["business_impact"]}}
