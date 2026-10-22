"""Générateur de trafic « boutique » EN CONTINU + page de contrôle des pannes (pensé pour Aiven Runtime).

Simule l'Online Boutique (mêmes services, mêmes noms d'opérations gRPC et mêmes tables que RCAEval) et écrit en
continu dans Aiven sous case_id='LIVE' : logs -> ClickHouse `rca_logs`, spans -> OpenSearch `otel-v1-apm-span-rcaeval`.
Une page web (port $PORT, 8080 par défaut) permet d'injecter / retirer des pannes, comme les feature flags de la
démo OpenTelemetry. C'est un SIMULATEUR de trafic de type OpenTelemetry, pas la vraie boutique.

Pannes : cpu (service saturé : ses appels sortants ralentissent), latency (latence réseau vers le service),
         error (le service renvoie des erreurs). L'état des pannes est enregistré dans PostgreSQL (`live_faults`)
         et sert de vérité terrain pour mesurer l'agent en direct.

  python -u -m scripts.traffic
"""
import json
import math
import os
import random
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

CASE = "LIVE"
INDEX = "otel-v1-apm-span-rcaeval"
TICK_S, REQ_PER_TICK, KEEP_MIN = 5, 40, 45
SERVICES = ["frontendservice", "productcatalogservice", "currencyservice", "cartservice", "recommendationservice",
            "adservice", "shippingservice", "checkoutservice", "paymentservice", "emailservice"]
FAULTABLE = SERVICES[1:]
KINDS = {"cpu": "CPU saturé", "latency": "Latence réseau", "error": "Erreurs"}
# (appelant, service appelé, méthode, durée moyenne ms côté serveur)
BROWSE = [("frontendservice", "productcatalogservice", "GetProduct", 1.5), ("frontendservice", "currencyservice", "Convert", 0.8),
          ("frontendservice", "recommendationservice", "ListRecommendations", 6), ("frontendservice", "adservice", "GetAds", 3),
          ("recommendationservice", "productcatalogservice", "ListProducts", 2)]
CART = [("frontendservice", "cartservice", "GetCart", 4), ("frontendservice", "currencyservice", "Convert", 0.8)]
CHECKOUT = [("frontendservice", "checkoutservice", "PlaceOrder", 0), ("checkoutservice", "cartservice", "GetCart", 4),
            ("checkoutservice", "productcatalogservice", "GetProduct", 1.5), ("checkoutservice", "shippingservice", "GetQuote", 3),
            ("checkoutservice", "shippingservice", "ShipOrder", 3), ("checkoutservice", "paymentservice", "Charge", 6),
            ("checkoutservice", "emailservice", "SendOrderConfirmation", 3), ("checkoutservice", "currencyservice", "Convert", 0.8)]
FLOWS = [(0.60, BROWSE), (0.25, CART), (0.15, CHECKOUT)]
_faults: dict[str, dict] = {}
_lock = threading.Lock()
_stats = {"ticks": 0, "logs": 0, "spans": 0, "last_error": None}


def camel(svc: str) -> str:
    return {"productcatalogservice": "ProductCatalog", "currencyservice": "Currency", "cartservice": "Cart",
            "recommendationservice": "Recommendation", "adservice": "Ad", "shippingservice": "Shipping",
            "checkoutservice": "Checkout", "paymentservice": "Payment", "emailservice": "Email"}[svc] + "Service"


def lognorm(mean: float, rnd: random.Random) -> float:
    return mean * math.exp(rnd.gauss(0, 0.25))


def gen_request(t0: datetime, rnd: random.Random, faults: dict):
    """-> (logs, spans) d'une requête utilisateur."""
    flow = rnd.choices([f for _, f in FLOWS], weights=[w for w, _ in FLOWS])[0]
    trace = uuid.uuid4().hex
    spans, logs, now_off = [], [], 0.0
    root = uuid.uuid4().hex[:16]
    total = 5.0
    ids = {}
    # durées serveur d'abord (PlaceOrder = somme de ses appels sortants)
    calls = []
    for caller, callee, method, mean in flow:
        fk = faults.get(callee, {}).get("kind")
        fc = faults.get(caller, {}).get("kind")
        server = lognorm(mean, rnd) if mean else 0.0
        if fk == "cpu":
            server *= 1.2
        client_extra = 1.0 + rnd.random()
        client_mult = 3.0 if fc == "cpu" else 1.0
        if fk == "latency":
            client_extra += 80 + rnd.random() * 40
        err = fk == "error" and rnd.random() < 0.5
        calls.append([caller, callee, method, server, client_extra, client_mult, err])
    sums = {}
    for c in calls:
        sums[c[0]] = sums.get(c[0], 0.0) + (c[3] + c[4]) * c[5]
    for c in calls:
        if c[2] == "PlaceOrder":
            c[3] = sums.get("checkoutservice", 0.0) + 5.0
    for caller, callee, method, server, extra, mult, err in calls:
        client = (server + extra) * (mult if method != "PlaceOrder" else 1.0)
        sid, cid = uuid.uuid4().hex[:16], uuid.uuid4().hex[:16]
        ts = t0 + timedelta(milliseconds=now_off)
        op = f"hipstershop.{camel(callee)}/{method}"
        parent = ids.get(caller, root)
        spans.append((caller, op, ts, client, err, cid, parent))
        spans.append((callee, "grpc." + op, ts, server, err, sid, cid))
        ids[callee] = sid
        now_off += 1.0
        total += client
        lvl = "error" if err else "info"
        logs.append((ts, callee, lvl, f"{method} {'failed' if err else 'ok'}", "/cart" if callee == "cartservice" else "/",
                     "rpc error: unavailable" if err else ""))
    spans.append(("frontendservice", "frontend", t0, total, False, root, ""))
    logs.append((t0, "frontendservice", "info", "request served", "/", ""))
    return logs, spans, trace


def build_batch(now: datetime, faults: dict, rnd: random.Random):
    logs, docs = [], []
    for _ in range(REQ_PER_TICK):
        t0 = now - timedelta(seconds=rnd.uniform(0, TICK_S))
        lg, sp, trace = gen_request(t0, rnd, faults)
        for ts, svc, lvl, msg, path, err in lg:
            logs.append([CASE, ts, svc, lvl, msg, path, err, 500 if (lvl == "error" or err) else 200])  # ts en UTC explicite (pas de naïf)
        for svc, op, ts, ms, err, sid, parent in sp:
            docs.append({"_index": INDEX, "_source": {
                "caseId": CASE, "traceId": trace, "spanId": sid, "parentSpanId": parent, "serviceName": svc,
                "operationName": op, "startTime": int(ts.timestamp() * 1000), "durationInNanos": int(ms * 1e6),
                "status": {"code": "ERROR" if err else "OK"}}})
    return logs, docs


# ---------- Pannes (état + vérité terrain PostgreSQL) ----------
def _pg():
    import psycopg

    return psycopg.connect(os.environ["POSTGRES_URL"], autocommit=True, connect_timeout=5)


def _init_pg():
    with _pg() as c:
        c.execute("CREATE TABLE IF NOT EXISTS live_faults (id serial PRIMARY KEY, service text NOT NULL, kind text NOT NULL, "
                  "since timestamptz NOT NULL DEFAULT now(), cleared_at timestamptz)")
        for sid, svc, kind, since in c.execute("SELECT id, service, kind, since FROM live_faults WHERE cleared_at IS NULL"):
            _faults[svc] = {"kind": kind, "since": since.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"), "id": sid}


def set_fault(service: str, kind: str):
    if service not in FAULTABLE or kind not in KINDS:
        raise ValueError("service ou panne inconnu")
    with _lock:
        if service in _faults:
            return
        with _pg() as c:
            fid = c.execute("INSERT INTO live_faults (service, kind) VALUES (%s, %s) RETURNING id", (service, kind)).fetchone()[0]
        _faults[service] = {"kind": kind, "since": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"), "id": fid}


def clear_faults():
    with _lock:
        with _pg() as c:
            c.execute("UPDATE live_faults SET cleared_at = now() WHERE cleared_at IS NULL")
        _faults.clear()


# ---------- Page de contrôle ----------
PAGE = """<!doctype html><meta charset=utf-8><title>Traffic simulator</title>
<style>body{font:15px system-ui;background:#0f1218;color:#e6e9ef;max-width:760px;margin:32px auto;padding:0 16px}
h1{font-size:20px}small{color:#8b93a7}table{width:100%;border-collapse:collapse;margin-top:16px}td{padding:8px;border-bottom:1px solid #242a36}
button{background:#1c2230;color:#e6e9ef;border:1px solid #333b4d;border-radius:6px;padding:5px 10px;cursor:pointer}button:hover{border-color:#ff5a5f}
.on{color:#ff6b73;font-weight:600}</style>
<h1>Simulateur de trafic · pannes</h1>
<small>Trafic de type Online Boutique écrit en continu dans Aiven (ClickHouse + OpenSearch). Simulateur, pas la vraie boutique.</small>
<div id=s></div><table id=t></table><p><button onclick="act('/clear')">Tout rétablir</button></p>
<script>
const K={cpu:'CPU saturé',latency:'Latence réseau',error:'Erreurs'};
async function act(p){await fetch(p,{method:'POST'});load()}
async function load(){const d=await (await fetch('/state')).json();
document.getElementById('s').innerHTML='<p>Écrit : '+d.stats.logs+' logs, '+d.stats.spans+' spans'+(d.stats.last_error?' · <span class=on>'+d.stats.last_error+'</span>':'')+'</p>';
document.getElementById('t').innerHTML=d.services.map(s=>'<tr><td>'+s+'</td><td>'+(d.faults[s]?'<span class=on>'+K[d.faults[s].kind]+'</span> <small>depuis '+d.faults[s].since+' UTC</small>':'<small>sain</small>')+'</td><td>'+
Object.keys(K).map(k=>'<button onclick="act(\\'/fault?service='+s+'&kind='+k+'\\')">'+K[k]+'</button>').join(' ')+'</td></tr>').join('')}
load();setInterval(load,3000)
</script>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code); self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/state":
            self._send(200, json.dumps({"services": FAULTABLE, "faults": _faults, "stats": _stats}))
        else:
            self._send(200, PAGE, "text/html")

    def do_POST(self):
        u = urlparse(self.path); q = {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            if u.path == "/fault":
                set_fault(q.get("service", ""), q.get("kind", ""))
            elif u.path == "/clear":
                clear_faults()
            self._send(200, json.dumps({"ok": True, "faults": _faults}))
        except Exception as e:
            self._send(400, json.dumps({"ok": False, "error": str(e)}))


def cleanup(ch, osc, now: datetime):
    cutoff = now - timedelta(minutes=KEEP_MIN)
    ch.command("ALTER TABLE rca_logs DELETE WHERE case_id = 'LIVE' AND ts < {c:DateTime64(3)}",
               parameters={"c": cutoff})
    osc.delete_by_query(index=INDEX, conflicts="proceed", ignore=[404], body={"query": {"bool": {"filter": [
        {"term": {"caseId": CASE}}, {"range": {"startTime": {"lt": int(cutoff.timestamp() * 1000)}}}]}}})


def main():
    from opensearchpy import OpenSearch, helpers

    from backend.tools import clickhouse_client

    _init_pg()
    ch, osc, rnd = clickhouse_client(), OpenSearch(hosts=[os.environ["OPENSEARCH_URL"]], timeout=60), random.Random()
    srv = ThreadingHTTPServer(("0.0.0.0", int(os.getenv("PORT", "8080"))), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print("trafic démarré ; page de contrôle sur le port", os.getenv("PORT", "8080"), flush=True)
    last_clean = 0.0
    while True:
        t = time.time()
        try:
            now = datetime.now(timezone.utc)
            with _lock:
                faults = dict(_faults)
            logs, docs = build_batch(now, faults, rnd)
            ch.insert("rca_logs", logs, column_names=["case_id", "ts", "service_name", "level", "message", "req_path", "error", "status_code"])
            helpers.bulk(osc, docs, request_timeout=60)
            _stats.update(ticks=_stats["ticks"] + 1, logs=_stats["logs"] + len(logs), spans=_stats["spans"] + len(docs), last_error=None)
            if t - last_clean > 600:
                cleanup(ch, osc, now); last_clean = t
        except Exception as e:
            _stats["last_error"] = str(e)[:120]
            print("erreur:", e, flush=True)
        time.sleep(max(0.0, TICK_S - (time.time() - t)))


if __name__ == "__main__":
    main()
