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
