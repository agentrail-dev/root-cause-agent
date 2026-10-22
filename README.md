# Root Cause Agent

Agent IA qui diagnostique un incident de production en corrélant ClickHouse (logs), OpenSearch (traces) et PostgreSQL (commandes) sur Aiven. Graphe LangGraph : `analyze_question → detect_anomaly → find_faulty_span → check_business_impact → synthesize`.

## Lancement local
```bash
python -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt
cp .env.example .env   # renseigner les URLs Aiven + ANTHROPIC_API_KEY
set -a; . ./.env; set +a
python -m scripts.seed_data                  # idempotent (DROP + CREATE)
uvicorn backend.main:app --port 8000   # UI sur http://localhost:8000
```

## Données

| Source | Contenu | Où |
|---|---|---|
| **RCAEval RE2-OB** (Online Boutique) | 90 pannes réelles annotées (30 types × 3 répétitions) : 15 053 223 logs, 34 461 235 spans | ClickHouse `rca_logs` + `rca_cases`, OpenSearch `otel-v1-apm-span-rcaeval` |
| **Scénario synthétique live** | service `payment-processor` : pic de 5xx, spans lents, commandes bloquées | ClickHouse `logs`, OpenSearch `otel-v1-apm-span-demo`, PostgreSQL `orders` |
| **Alertes du watchdog** | historique des incidents diagnostiqués | PostgreSQL `alerts` |

Chargement RCAEval : `scripts/load_rcaeval.py` lit le zip sur Zenodo par plages HTTP et charge Aiven directement depuis le cloud (service Aiven Runtime `loader`, ~12 s par cas). Aucun cas n'est échantillonné : toutes les lignes sont chargées.

## Watchdog (surveillance continue)
`WATCHDOG=true` : toutes les `WATCHDOG_INTERVAL` secondes (60 par défaut) l'application lit la santé des services ; un nouvel incident déclenche seul l'agent, l'alerte (cause racine, impact, actions suggérées) est enregistrée dans PostgreSQL et envoyée à `SLACK_WEBHOOK_URL` si défini. Endpoints : `GET /alerts`, `GET /watchdog/status`, `POST /watchdog/tick`.

## Démo live (incident injecté)
```bash
python -m scripts.inject_incident --reset     # état sain (aucun incident)
python -m scripts.inject_incident             # injecte l'incident : le watchdog le détecte et diagnostique seul (~30 s)
python -m scripts.inject_incident --resolve   # trafic sain : l'alerte passe en « résolue »
```

## Benchmark RCAEval
`POST /benchmark` (ou `python -m scripts.run_benchmark`) diagnostique chaque cas **sans LLM** (reproductible) et compare le service trouvé à la vérité terrain ; le début de panne estimé est comparé à l'heure d'injection réelle.

## Mode fallback (sans base)
`DEMO_MODE=true` : les outils lisent `backend/fixtures/*.json`. Sans `ANTHROPIC_API_KEY`, des heuristiques remplacent les appels LLM (extraction de la question, rédaction de la synthèse).

## Docker / Aiven Runtime
```bash
docker build -t root-cause-agent .
docker run -p 8501:8501 --env-file .env root-cause-agent
```
Variables : `CLICKHOUSE_URL`, `OPENSEARCH_URL`, `POSTGRES_URL`, `ANTHROPIC_API_KEY`, `DEMO_MODE`.

## Tests
`pytest` (outils avec résumés purs + parcours bout-en-bout en DEMO_MODE).

## Métriques affichées
Temps de diagnostic mesuré (vs 45 min de référence humaine, hypothèse du pitch), nombre de preuves, accuracy = opération fautive trouvée == vérité terrain (`GROUND_TRUTH_OPERATION` ou fixture en DEMO_MODE).
