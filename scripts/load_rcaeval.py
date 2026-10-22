"""Charge RCAEval RE2 (un système, ex. RE2-OB) directement depuis Zenodo vers Aiven.

Conçu pour tourner DANS le cloud (conteneur Aiven Runtime) : lit le zip distant par plages HTTP,
décompresse en mémoire, insère logs -> ClickHouse (rca_logs), spans -> OpenSearch
(otel-v1-apm-span-rcaeval) et un manifeste de cas (rca_cases) avec la vérité terrain.
Idempotent et reprenable : un cas déjà présent dans rca_cases est sauté ; un cas partiel est purgé.

Usage : python -m scripts.load_rcaeval [--prefix RE2-OB] [--limit N] [--only CASE_ID]
"""
import argparse
import io
import os
import struct
import sys
import time
import zlib
from collections import defaultdict

import pandas as pd

from scripts.fetch_rcaeval import URL, get, read_index

INDEX = "otel-v1-apm-span-rcaeval"
LOG_SQL = ("CREATE TABLE IF NOT EXISTS rca_logs (case_id LowCardinality(String), ts DateTime64(9, 'UTC'), "
           "service_name LowCardinality(String), level LowCardinality(String), message String, "
           "req_path String, error String, status_code UInt16) "
           "ENGINE = MergeTree ORDER BY (case_id, service_name, ts)")
CASES_SQL = ("CREATE TABLE IF NOT EXISTS rca_cases (case_id String, system String, run UInt8, "
             "root_cause_service String, fault_type String, inject_ts DateTime('UTC'), "
             "start_ts DateTime64(3, 'UTC'), end_ts DateTime64(3, 'UTC'), n_logs UInt64, n_spans UInt64, "
             "loaded_at DateTime DEFAULT now()) ENGINE = MergeTree ORDER BY case_id")
MAPPING = {"settings": {"number_of_shards": 2, "number_of_replicas": 0, "refresh_interval": "60s"},
           "mappings": {"properties": {
               "caseId": {"type": "keyword"}, "traceId": {"type": "keyword"}, "spanId": {"type": "keyword"},
               "parentSpanId": {"type": "keyword"}, "serviceName": {"type": "keyword"},
               "operationName": {"type": "keyword"},
               "startTime": {"type": "date", "format": "epoch_millis"}, "durationInNanos": {"type": "long"},
               "status": {"properties": {"code": {"type": "keyword"}}}}}}


def member_bytes(name: str, csz: int, lho: int, comp: int) -> bytes:
    h = get((lho, lho + 29))
    nl, el = struct.unpack("<HH", h[26:30])
    start = lho + 30 + nl + el
    raw = b"".join(get((a, min(a + (8 << 20) - 1, start + csz - 1))) for a in range(start, start + csz, 8 << 20))
    return zlib.decompress(raw, -15) if comp == 8 else raw


def case_files(ents, prefix):
    cases = defaultdict(dict)
    for name, csz, lho, comp in ents:
        p = name.split("/")
        if len(p) == 5 and p[1] == prefix and p[4] in ("logs.csv", "traces.csv", "inject_time.txt"):
            cases[(p[2], p[3])][p[4]] = (name, csz, lho, comp)
    return cases


def load_case(ch, os_client, prefix, fault, run, files):
    from opensearchpy import helpers
    case_id = f"{prefix.split('-')[-1]}-{fault}-{run}"
    service, ftype = fault.rsplit("_", 1)
    ch.command("ALTER TABLE rca_logs DELETE WHERE case_id = {c:String} SETTINGS mutations_sync = 2", parameters={"c": case_id})
    os_client.delete_by_query(index=INDEX, body={"query": {"term": {"caseId": case_id}}}, conflicts="proceed",
                              refresh=True, ignore=[404])
    inject = int(member_bytes(*files["inject_time.txt"]).decode().strip())

    logs = pd.read_csv(io.BytesIO(member_bytes(*files["logs.csv"])), usecols=["timestamp", "container_name", "message", "level", "req_path", "error"],
                       dtype=str, keep_default_na=False)
    lv = logs["level"].str.lower()
    out = pd.DataFrame({
        "case_id": case_id,
        "ts": pd.to_datetime(logs["timestamp"].astype("int64"), unit="ns", utc=True).dt.tz_localize(None),
        "service_name": logs["container_name"], "level": lv, "message": logs["message"],
        "req_path": logs["req_path"], "error": logs["error"],
        "status_code": ((lv.isin(["error", "fatal", "critical", "panic"])) | (logs["error"] != "")).map({True: 500, False: 200}).astype("uint16")})
    for i in range(0, len(out), 200_000):
        ch.insert_df("rca_logs", out.iloc[i:i + 200_000])

    tr = pd.read_csv(io.BytesIO(member_bytes(*files["traces.csv"])), usecols=["traceID", "spanID", "serviceName", "operationName",
                     "startTimeMillis", "duration", "statusCode", "parentSpanID"], dtype=str, keep_default_na=False)

    def docs():
        for t in tr.itertuples(index=False):
            ok = t.statusCode in ("", "0", "0.0")
            yield {"_index": INDEX, "_source": {
                "caseId": case_id, "traceId": t.traceID, "spanId": t.spanID, "parentSpanId": t.parentSpanID,
                "serviceName": t.serviceName, "operationName": t.operationName, "startTime": int(t.startTimeMillis),
                "durationInNanos": int(float(t.duration) * 1000), "status": {"code": "OK" if ok else "ERROR"}}}
    for ok, _ in helpers.parallel_bulk(os_client, docs(), thread_count=4, chunk_size=5000, request_timeout=120):
        pass

    ch.insert("rca_cases", [[case_id, prefix, int(run), service, ftype, inject, out["ts"].min(), out["ts"].max(), len(out), len(tr)]],
              column_names=["case_id", "system", "run", "root_cause_service", "fault_type", "inject_ts", "start_ts", "end_ts", "n_logs", "n_spans"])
    return case_id, len(out), len(tr)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--prefix", default="RE2-OB")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--only", default="")
    a = ap.parse_args()
    from opensearchpy import OpenSearch
    from backend.tools import clickhouse_client
    ch, osc = clickhouse_client(), OpenSearch(hosts=[os.environ["OPENSEARCH_URL"]], timeout=120)
    ch.command(LOG_SQL); ch.command(CASES_SQL)
    if not osc.indices.exists(index=INDEX):
        osc.indices.create(index=INDEX, body=MAPPING)
    done = {r[0] for r in ch.query("SELECT case_id FROM rca_cases").result_rows}
    _, ents = read_index()
    cases = case_files(ents, a.prefix)
    todo = sorted(cases)
    print(f"{len(todo)} cas dans {a.prefix}, {len(done)} déjà chargés", flush=True)
    n = 0
    for fault, run in todo:
        cid = f"{a.prefix.split('-')[-1]}-{fault}-{run}"
        if cid in done or (a.only and cid != a.only):
            continue
        if a.limit and n >= a.limit:
            break
        t0 = time.time()
        try:
            _, nl, ns = load_case(ch, osc, a.prefix, fault, run, cases[(fault, run)])
            print(f"OK {cid}: {nl} logs, {ns} spans en {time.time() - t0:.0f}s", flush=True)
            n += 1
        except Exception as e:
            print(f"ECHEC {cid}: {e}", flush=True)
    osc.indices.put_settings(index=INDEX, body={"index": {"refresh_interval": "1s"}})
    print("terminé", flush=True)


if __name__ == "__main__":
    main()
