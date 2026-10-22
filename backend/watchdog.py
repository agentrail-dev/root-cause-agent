"""Watchdog : surveille la santé des services en continu et diagnostique seul les nouveaux incidents.

Cycle (toutes les WATCHDOG_INTERVAL secondes, 60 par défaut) :
  1. tools.services_health()  -> statut par service (ClickHouse)
  2. service healthy -> incident : lance l'agent (run_stream), enregistre une alerte avec la cause racine
  3. notification optionnelle : POST JSON {"text": ...} vers SLACK_WEBHOOK_URL (compatible Slack/Mattermost)
  4. service incident -> healthy : l'alerte est marquée résolue (durée d'incident calculée)

Un incident déjà ouvert n'est ni re-diagnostiqué ni re-notifié. L'historique est en mémoire (50 alertes).
"""
import os
import threading
import time
from collections import deque
from datetime import datetime, timezone

import requests

from . import tools
from .agent import run_stream

_lock = threading.Lock()
_alerts: deque = deque(maxlen=50)
_open: dict[str, dict] = {}  # service -> alerte ouverte
_status = {"running": False, "last_tick": None, "last_error": None, "interval": None, "ticks": 0}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def notify(alert: dict) -> bool:
    url = os.getenv("SLACK_WEBHOOK_URL")
    if not url:
        return False
    rc, m = alert["root_cause"], alert["metrics"]
    text = (f":rotating_light: *Incident {alert['service']}* ouvert à {alert['opened_at']} UTC\n"
            f"*Cause racine* : {rc['summary']}\n*Impact* : {rc['business_impact']}\n"
            f"_Diagnostic automatique en {m['diagnosis_seconds']} s._")
    try:
        return requests.post(url, json={"text": text}, timeout=10).ok
    except requests.RequestException:
        return False


def diagnose(service: str) -> dict:
    final = next(e for e in run_stream(f"Diagnostiquer l'incident sur {service}", service=service) if e["type"] == "final")
    return final


def tick() -> list[dict]:
    """Un cycle de surveillance ; retourne les alertes créées pendant ce cycle."""
    created = []
    health = tools.services_health()
    for s in health:
        svc = s["service"]
        with _lock:
            is_open = svc in _open
        if s["status"] == "incident" and not is_open:
            final = diagnose(svc)
            alert = {"id": f"{svc}-{int(time.time())}", "service": svc, "opened_at": _now(), "resolved_at": None,
                     "error_rate": s["recent_error_rate"], "root_cause": final["root_cause"],
                     "metrics": final["metrics"], "evidence": final["evidence"], "notified": False}
            alert["notified"] = notify(alert)
            with _lock:
                _open[svc] = alert
                _alerts.appendleft(alert)
            created.append(alert)
        elif s["status"] == "healthy" and is_open:
            with _lock:
                _open.pop(svc)["resolved_at"] = _now()
    with _lock:
        _status.update(last_tick=_now(), ticks=_status["ticks"] + 1, last_error=None)
    return created


def _loop(interval: int):
    while True:
        try:
            tick()
        except Exception as e:  # une source indisponible ne doit pas tuer la surveillance
            with _lock:
                _status.update(last_error=str(e), last_tick=_now())
        time.sleep(interval)


def start() -> bool:
    """Démarre la boucle si WATCHDOG=true (ou 1). Idempotent."""
    if os.getenv("WATCHDOG", "false").lower() not in ("1", "true", "yes") or _status["running"]:
        return False
    interval = int(os.getenv("WATCHDOG_INTERVAL", "60"))
    _status.update(running=True, interval=interval)
    threading.Thread(target=_loop, args=(interval,), daemon=True, name="watchdog").start()
    return True


def alerts() -> list[dict]:
    with _lock:
        return list(_alerts)


def status() -> dict:
    with _lock:
        return {**_status, "open_incidents": sorted(_open), "webhook_configured": bool(os.getenv("SLACK_WEBHOOK_URL"))}


def reset():  # tests
    with _lock:
        _alerts.clear(); _open.clear(); _status.update(ticks=0, last_tick=None, last_error=None)
