"""Seed idempotent du scénario d'incident synthétique (option B).

Usage : python -m scripts.seed_data   (variables CLICKHOUSE_URL, OPENSEARCH_URL, POSTGRES_URL)
Scénario : service payment-processor ; incident démarré à now()-10 min.
"""
import os
import random
import uuid
from datetime import datetime, timedelta, timezone

SERVICE = "payment-processor"
N_NORMAL_LOGS, N_ERROR_LOGS, N_ERROR_SPANS, N_PENDING = 1000, 150, 1250, 500


def gen(now: datetime):
    rnd = random.Random(42)
    start = now - timedelta(minutes=10)
    logs = []
    for _ in range(N_NORMAL_LOGS):  # trafic normal sur 60 min, ~1% d'erreurs de fond
        ts = now - timedelta(seconds=rnd.uniform(0, 3600))
        logs.append((ts, SERVICE, 500 if rnd.random() < 0.01 else 200))
    for _ in range(N_ERROR_LOGS):
        logs.append((start + timedelta(seconds=rnd.uniform(0, 600)), SERVICE, 500))
    for svc in ("checkout-api", "inventory-service", "auth-service"):  # services sains (contexte)
        for _ in range(300):
            logs.append((now - timedelta(seconds=rnd.uniform(0, 3600)), svc, 500 if rnd.random() < 0.01 else 200))
    spans = []
    for i in range(N_ERROR_SPANS):
        ts = start + timedelta(seconds=rnd.uniform(0, 600))
        spans.append({"traceId": uuid.uuid4().hex, "spanId": uuid.uuid4().hex[:16], "serviceName": SERVICE,
                      "operationName": "db.query.orders", "startTime": ts.isoformat(),
                      "durationInNanos": int(rnd.uniform(2.1, 6.0) * 1e9), "status.code": "ERROR"})
    for i in range(200):  # bruit : opérations saines et rapides / quelques lentes
        ts = now - timedelta(seconds=rnd.uniform(0, 3600))
        spans.append({"traceId": uuid.uuid4().hex, "spanId": uuid.uuid4().hex[:16], "serviceName": SERVICE,
                      "operationName": rnd.choice(["http.post.charge", "cache.get.session"]),
                      "startTime": ts.isoformat(), "durationInNanos": int(rnd.uniform(0.02, 2.4) * 1e9),
                      "status.code": "OK"})
    orders = [(i + 1, rnd.randint(1, 5000), "pending", start + timedelta(seconds=rnd.uniform(1, 600)))
              for i in range(N_PENDING)]
    orders += [(N_PENDING + i + 1, rnd.randint(1, 5000), "completed", now - timedelta(seconds=rnd.uniform(0, 3600)))
               for i in range(300)]
    return logs, spans, orders


def seed_clickhouse(logs):
    from backend.tools import clickhouse_client
    c = clickhouse_client()
    c.command("DROP TABLE IF EXISTS logs")
    c.command("CREATE TABLE logs (timestamp DateTime, service_name String, status_code UInt16) "
              "ENGINE = MergeTree ORDER BY (service_name, timestamp)")
    c.insert("logs", [list(r) for r in logs], column_names=["timestamp", "service_name", "status_code"])


def seed_opensearch(spans):
    from opensearchpy import OpenSearch, helpers
    c = OpenSearch(hosts=[os.environ["OPENSEARCH_URL"]])
    index = "otel-v1-apm-span-demo"
    c.indices.delete(index=index, ignore_unavailable=True)
    c.indices.create(index=index, body={"mappings": {"properties": {
        "traceId": {"type": "keyword"}, "spanId": {"type": "keyword"}, "serviceName": {"type": "keyword"},
        "operationName": {"type": "keyword"}, "startTime": {"type": "date"},
        "durationInNanos": {"type": "long"}, "status": {"properties": {"code": {"type": "keyword"}}}}}})
    docs = ({"_index": index, "_source": {**{k: v for k, v in s.items() if k != "status.code"},
                                          "status": {"code": s["status.code"]}}} for s in spans)
    helpers.bulk(c, docs, refresh=True)


def seed_postgres(orders):
    import psycopg
    with psycopg.connect(os.environ["POSTGRES_URL"]) as conn:
        conn.execute("DROP TABLE IF EXISTS orders")
        conn.execute("CREATE TABLE orders (id int PRIMARY KEY, user_id int, status text, created_at timestamptz)")
        with conn.cursor() as cur:
            cur.executemany("INSERT INTO orders VALUES (%s,%s,%s,%s)", orders)


if __name__ == "__main__":
    now = datetime.now(timezone.utc)
    logs, spans, orders = gen(now)
    seed_clickhouse(logs)
    seed_opensearch(spans)
    seed_postgres(orders)
    print(f"Seed OK : {len(logs)} logs, {len(spans)} spans, {len(orders)} commandes")
