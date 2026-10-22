"""Injecte (ou résout) un incident EN DIRECT sur les bases Aiven, sans rien supprimer. Pour la démo/vidéo.

  python -m scripts.inject_incident            # incident payment-processor : pic de 5xx, spans lents, commandes bloquées
  python -m scripts.inject_incident --reset    # repart d'un état SAIN (recrée les tables de démo, sans incident)
  python -m scripts.inject_incident --resolve  # trafic sain : le taux d'erreur retombe, le watchdog résout l'alerte

Variables : CLICKHOUSE_URL, OPENSEARCH_URL, POSTGRES_URL (comme pour seed_data).
"""
import argparse
import random
import uuid
from datetime import datetime, timedelta, timezone

from scripts.seed_data import SERVICE

OPS_NORMAL = ["http.post.charge", "cache.get.session"]


def inject(now: datetime):
    import psycopg
    from opensearchpy import OpenSearch, helpers
    import os
    from backend.tools import clickhouse_client

    rnd = random.Random()
    start = now - timedelta(minutes=3)
    ch = clickhouse_client()
    logs = [[start + timedelta(seconds=rnd.uniform(0, 180)), SERVICE, 500] for _ in range(150)]
    logs += [[now - timedelta(seconds=rnd.uniform(0, 600)), SERVICE, 200] for _ in range(60)]
    ch.insert("logs", logs, column_names=["timestamp", "service_name", "status_code"])

    index = "otel-v1-apm-span-demo"
    osc = OpenSearch(hosts=[os.environ["OPENSEARCH_URL"]], timeout=60)
    spans = ({"_index": index, "_source": {
        "traceId": uuid.uuid4().hex, "spanId": uuid.uuid4().hex[:16], "serviceName": SERVICE,
        "operationName": "db.query.orders", "startTime": (start + timedelta(seconds=rnd.uniform(0, 180))).isoformat(),
        "durationInNanos": int(rnd.uniform(2.1, 6.0) * 1e9), "status": {"code": "ERROR"}}} for _ in range(1250))
    helpers.bulk(osc, spans, refresh=True)

    with psycopg.connect(os.environ["POSTGRES_URL"], autocommit=True) as conn:
        base = conn.execute("SELECT coalesce(max(id), 0) FROM orders").fetchone()[0]
        with conn.cursor() as cur:
            cur.executemany("INSERT INTO orders VALUES (%s,%s,%s,%s)",
                            [(base + i + 1, rnd.randint(1, 5000), "pending", start + timedelta(seconds=rnd.uniform(0, 180)))
                             for i in range(500)])
    print(f"Incident injecté sur {SERVICE} : 150 erreurs 5xx, 1250 spans lents, 500 commandes pending.")


def reset(now: datetime):
    """Recrée les tables de démo avec uniquement le trafic sain (le scénario seed moins l'incident)."""
    from scripts import seed_data as sd

    logs, spans, orders = sd.gen(now)
    logs = logs[:sd.N_NORMAL_LOGS] + logs[sd.N_NORMAL_LOGS + sd.N_ERROR_LOGS:]  # sans les 150 erreurs
    sd.seed_clickhouse(logs)
    sd.seed_opensearch(spans[sd.N_ERROR_SPANS:])                                  # sans les 1250 spans en erreur
    sd.seed_postgres(orders[sd.N_PENDING:])                                       # sans les 500 pending
    print("État sain recréé : aucun incident ouvert.")


def resolve(now: datetime):
    from backend.tools import clickhouse_client

    rnd = random.Random()
    logs = [[now - timedelta(seconds=rnd.uniform(0, 540)), SERVICE, 200] for _ in range(3000)]
    clickhouse_client().insert("logs", logs, column_names=["timestamp", "service_name", "status_code"])
    print(f"Trafic sain ajouté pour {SERVICE} : le taux d'erreur de la fenêtre récente retombe sous le seuil.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--resolve", action="store_true")
    ap.add_argument("--reset", action="store_true")
    a = ap.parse_args()
    (reset if a.reset else resolve if a.resolve else inject)(datetime.now(timezone.utc))
