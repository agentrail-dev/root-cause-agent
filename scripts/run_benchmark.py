"""Benchmark avant/après sur les cas RCAEval chargés dans Aiven + trace de la couche données.

  python -m scripts.run_benchmark [--limit N] [--out results]

Avant : ce que fait un ingénieur d'abord — chercher le service qui crache le plus de 5xx dans les logs.
Après : l'agent (comparaison avant/après panne sur les traces, sans LLM pour la reproductibilité).
Écrit results/benchmark.csv, results/benchmark.md et results/data_layer_in_action.txt.
"""
import argparse
import csv
import os
import time
from collections import defaultdict
from pathlib import Path
from urllib.parse import unquote

from backend import agent, tools


def load_env(path=".env"):
    for line in Path(path).read_text().splitlines():  # pas de `source` : mots de passe encodés URL
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def naive_logs_baseline(case_id: str) -> str | None:
    """Service avec le plus d'erreurs 5xx en plus pendant l'incident ; None si aucun log d'erreur."""
    svcs = tools.logs_scan(case_id)["services"]
    top = svcs[0] if svcs else None
    return top["service"] if top and top["errors_after"] > top["errors_before"] else None


def naive_latency_baseline(case_id: str) -> str | None:
    """Service dont la latence moyenne (toutes opérations confondues) a le plus augmenté avant -> après."""
    b0, b1, a0, a1 = tools.case_periods(case_id)
    osc = tools._opensearch()
    before, after = tools._op_stats(osc, case_id, b0, b1), tools._op_stats(osc, case_id, a0, a1)

    def mean_by_service(stats):
        tot = defaultdict(lambda: [0, 0.0])
        for (svc, _op), (n, mean, _err) in stats.items():
            tot[svc][0] += n
            tot[svc][1] += n * mean
        return {s: t / n for s, (n, t) in tot.items() if n}

    mb, ma = mean_by_service(before), mean_by_service(after)
    ratios = {s: ma[s] / mb[s] for s in ma if mb.get(s)}
    return max(ratios, key=ratios.get) if ratios else None


def norm(s):
    return tools.normalize_service(s) if s else None


def run(limit):
    rows = []
    for c in tools.list_cases()[: limit or None]:
        cid, truth = c["case_id"], c["root_cause_service"]
        t0 = time.perf_counter()
        base = naive_logs_baseline(cid)
        t_base = time.perf_counter() - t0
        lat = naive_latency_baseline(cid)
        final = next(e for e in agent.run_stream("", case_id=cid, use_llm=False) if e["type"] == "final")
        rows.append({"case_id": cid, "fault_type": c["fault_type"], "truth": truth,
                     "baseline_found": base or "(aucun log d'erreur)", "baseline_ok": int(norm(base) == norm(truth)),
                     "baseline_s": round(t_base, 1),
                     "latency_found": lat or "-", "latency_ok": int(norm(lat) == norm(truth)),
                     "agent_found": final["service"], "agent_ok": int(final["metrics"]["accuracy"] == 1.0),
                     "agent_s": final["metrics"]["diagnosis_seconds"],
                     "onset_error_s": final["metrics"]["onset_error_seconds"]})
        print(rows[-1], flush=True)
    return rows


def write_reports(rows, out: Path):
    out.mkdir(exist_ok=True)
    with open(out / "benchmark.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    by = defaultdict(list)
    for r in rows:
        by[r["fault_type"]].append(r)
    pct = lambda xs, k: f"{100 * sum(x[k] for x in xs) / len(xs):.0f}%"
    onset = [r["onset_error_s"] for r in rows if r["onset_error_s"] is not None]
    md = [f"# Benchmark RCAEval RE2-OB — {len(rows)} cas, vérité terrain connue\n",
          "| Type de panne | Cas | Grep des logs 5xx | Latence moyenne par service | Agent |", "|---|---|---|---|---|"]
    for ft, xs in sorted(by.items()):
        md.append(f"| {ft} | {len(xs)} | {pct(xs, 'baseline_ok')} | {pct(xs, 'latency_ok')} | {pct(xs, 'agent_ok')} |")
    md.append(f"| **Tous** | **{len(rows)}** | **{pct(rows, 'baseline_ok')}** | **{pct(rows, 'latency_ok')}** | **{pct(rows, 'agent_ok')}** |")
    md.append("\nLe grep des logs 5xx est la première étape d'un ingénieur, pas un concurrent sérieux : la plupart des "
              "pannes (cpu, mem, disk, delay...) ne produisent aucune erreur, et le frontend est seul à en logger.")
    md.append(f"\nTemps moyen : avant {sum(r['baseline_s'] for r in rows) / len(rows):.1f} s (recherche automatisée, "
              f"hors temps humain), agent {sum(r['agent_s'] for r in rows) / len(rows):.1f} s.")
    if onset:
        md.append(f"Erreur médiane sur l'instant de début de panne : {sorted(onset)[len(onset) // 2]:.0f} s "
                  f"({len(onset)} cas avec début détecté).")
    (out / "benchmark.md").write_text("\n".join(md) + "\n")
    print("\n".join(md))


def trace_data_layer(case_id: str, out: Path):
    """Rejoue une investigation en journalisant chaque requête réellement envoyée aux 3 bases Aiven."""
    log = []
    import clickhouse_connect.driver.client as chc
    import opensearchpy

    orig_q, orig_s = chc.Client.query, opensearchpy.OpenSearch.search

    def q(self, query, *a, **kw):
        t0 = time.perf_counter()
        r = orig_q(self, query, *a, **kw)
        log.append(("ClickHouse (logs)", " ".join(query.split()), f"{len(r.result_rows)} lignes", time.perf_counter() - t0))
        return r

    def s(self, *a, **kw):
        t0 = time.perf_counter()
        r = orig_s(self, *a, **kw)
        log.append(("OpenSearch (spans)", str(kw.get("body")), f"{r['hits']['total']['value']} spans (total plafonné à 10000)", time.perf_counter() - t0))
        return r

    chc.Client.query, opensearchpy.OpenSearch.search = q, s
    try:
        final = next(e for e in agent.run_stream("", case_id=case_id, use_llm=False) if e["type"] == "final")
    finally:
        chc.Client.query, opensearchpy.OpenSearch.search = orig_q, orig_s
    info = tools.case_info(case_id)
    lines = [f"THE DATA LAYER IN ACTION — cas {case_id}",
             f"Panne injectée (vérité terrain) : {info['root_cause_service']} / {info['fault_type']} à {info['inject_ts']}",
             f"Volumes : {info['n_logs']} logs -> ClickHouse (Aiven), {info['n_spans']} spans -> OpenSearch (Aiven), "
             "manifeste des cas -> ClickHouse (rca_cases)", "",
             "Requêtes réellement exécutées par l'agent :", ""]
    for i, (db, query, res, dt) in enumerate(log, 1):
        lines += [f"[{i}] {db} — {res} — {dt * 1000:.0f} ms", query[:1500], ""]
    lines += [f"Cause racine trouvée : {final['service']}  (vérité : {info['root_cause_service']})",
              f"Diagnostic en {final['metrics']['diagnosis_seconds']} s", ""]
    (out / "data_layer_in_action.txt").write_text("\n".join(lines))
    print(f"{len(log)} requêtes tracées")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--out", default="results")
    ap.add_argument("--trace-case", default="")
    a = ap.parse_args()
    load_env()
    out = Path(a.out)
    out.mkdir(exist_ok=True)
    first = a.trace_case or tools.list_cases()[0]["case_id"]
    trace_data_layer(first, out)
    write_reports(run(a.limit), out)
