# Root Cause Agent

> **Le watchdog IA de votre production : il surveille en continu vos logs, traces et commandes sur Aiven et, dès qu'un service dérape, nomme tout seul le coupable, avec les preuves et l'impact métier.**

Un agent IA qui trouve la cause racine d'un incident de production en **corrélant logs, traces et données métier** stockés dans trois bases managées **Aiven**. Un watchdog surveille en continu : un nouvel incident est diagnostiqué seul, sans que personne ne pose de question.

> Problème client : un ingénieur d'astreinte SRE saute entre un outil de logs, un outil de traces et la base métier pour savoir *quel service* est en cause et *qui est touché*. Hypothèse de travail du pitch : environ 45 minutes par incident (hypothèse, pas une mesure).

## Liens

| | |
|---|---|
| Application (Aiven Runtime) | https://01a16335-e6b3-7bfd-b4ef-381f3902c65c-8501.eur-1.aiven.app/ |
| Page de contrôle des pannes (simulateur de trafic) | https://01a1637c-3137-7837-bc71-5e1fa8e77400-8080.eur-1.aiven.app/ |
| Dépôt | https://github.com/agentrail-dev/root-cause-agent |
| Pitch (3 min) / script vidéo (90 s) | [`docs/PITCH.md`](docs/PITCH.md) / [`docs/DEMO_SCRIPT.md`](docs/DEMO_SCRIPT.md) |
| Résultats détaillés du benchmark | [`results/benchmark.md`](results/benchmark.md) |

## Preuve chiffrée

Benchmark sur **90 pannes réelles annotées** (RCAEval RE2-OB, vérité terrain connue, exécuté **sans LLM** pour être reproductible : `python -m scripts.run_benchmark`).

| Méthode | Bon service trouvé |
|---|---|
| **Agent** | **81 % (73/90)** |
| Base « latence moyenne la plus haute par service » | 58 % |
| Grep des logs 5xx (première étape manuelle) | 0 % — base naïve : 74 cas sur 90 ne produisent aucun log d'erreur |

Par type de panne : delay 100 %, cpu 93 %, disk 93 %, mem 93 %, loss 73 %, **socket 33 %** (point faible assumé). Erreur médiane sur l'instant de début de panne : 25 s. Diagnostic de l'agent : 3,9 s en moyenne.

En direct : sur le simulateur, une panne injectée a produit une alerte watchdog en environ 75 s, avec le bon service et un début estimé à 19 s près (**un seul essai**, pas une moyenne).

## Ce qui est réel et ce qui est simulé

- **Benchmark : données réelles RCAEval** (Online Boutique, 90 pannes). 15 053 223 logs et 34 461 235 spans, **tout est chargé, rien n'est échantillonné**.
- **Démo live : un simulateur de trafic de type OpenTelemetry**, pas la vraie boutique OpenTelemetry. Il tourne sur Aiven Runtime, écrit en continu dans les mêmes tables et permet d'injecter une panne (CPU saturé, latence réseau, erreurs).
- L'ancien scénario synthétique `payment-processor` (`scripts/seed_data.py`, `scripts/inject_incident.py`) reste disponible mais n'est plus le chemin principal.

## Architecture

```
Simulateur (Runtime) ─┐                       ┌─ ClickHouse  : logs (rca_logs, rca_cases)
RCAEval (loader) ─────┼─► Aiven ──────────────┼─ OpenSearch  : traces (otel-v1-apm-span-rcaeval)
                      │                       └─ PostgreSQL  : commandes, alertes watchdog, pannes
                      ▼
        FastAPI + agent LangGraph  ──►  Interface web (servie par FastAPI)
        analyze_question → detect_anomaly → find_faulty_span → check_business_impact → synthesize
        + watchdog (boucle de surveillance)       Claude : rédaction de la synthèse
```

**Méthode de cause racine (déterministe, sans LLM)** : la fenêtre est comparée avant/après sur les traces, avec trois signaux — opérations *servies* par le service ralenties, appels *sortants* de plusieurs cibles ralentis (service saturé), appels *entrants* ralentis vus d'appelants sains (latence côté service). Le service au score le plus fort est retenu ; le début de panne est estimé sur la série de l'opération la plus dégradée. Claude ne fait que **rédiger** la synthèse et les actions suggérées à partir de ces preuves (repli déterministe si l'API est indisponible).

**Les données sont indispensables** : sans les trois bases Aiven, l'agent ne peut pas conclure (le mode `DEMO_MODE` ne sert qu'à un repli de secours avec des fichiers pré-calculés).

## API

| Route | Rôle |
|---|---|
| `GET /health` | état et mode |
| `GET /incidents` | santé des services (bascule en « live » quand le simulateur écrit) |
| `POST /diagnose` | diagnostic (flux NDJSON) : `service`, `case_id` ou question libre |
| `GET /cases`, `POST /benchmark` | cas RCAEval chargés, évaluation sur tous les cas |
| `GET /alerts`, `GET /watchdog/status`, `POST /watchdog/tick` | alertes du watchdog (persistées dans PostgreSQL), état, cycle forcé |

## Watchdog

`WATCHDOG=true` : toutes les `WATCHDOG_INTERVAL` secondes (60 par défaut), l'application lit la santé des services. Un nouvel incident déclenche l'agent ; l'alerte (cause racine, impact, actions suggérées, vérité terrain si connue) est enregistrée dans PostgreSQL (table `alerts`, elle survit aux redémarrages). `SLACK_WEBHOOK_URL`, s'il est défini, reçoit le message (compatible Slack / Mattermost) ; rien n'est envoyé sinon.

## Démo en direct

1. Ouvrir la page de contrôle des pannes et le dashboard.
2. « CPU saturé » sur `checkoutservice` : aucun log d'erreur, un grep des 5xx ne trouverait rien.
3. Le watchdog détecte, l'agent diagnostique, l'alerte apparaît (environ 75 s).
4. « Tout rétablir » : l'alerte passe en résolue.

## Lancer le projet

```bash
python -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt
cp .env.example .env     # renseigner CLICKHOUSE_URL, OPENSEARCH_URL, POSTGRES_URL, ANTHROPIC_API_KEY
uvicorn backend.main:app --port 8000        # interface et API sur http://localhost:8000
pytest                                       # tests (sans base : fixtures et fonctions pures)
```

Variables : `CLICKHOUSE_URL`, `OPENSEARCH_URL`, `POSTGRES_URL`, `ANTHROPIC_API_KEY`, `ANTHROPIC_MODEL`, `WATCHDOG`, `WATCHDOG_INTERVAL`, `SLACK_WEBHOOK_URL`, `DEMO_MODE`. **Aucun secret n'est dans le dépôt.**

## Déploiement (Aiven Runtime)

Trois applications, construites depuis ce dépôt (intégration GitHub d'Aiven) :

| Service | Dockerfile | Rôle |
|---|---|---|
| `app` | `Dockerfile` | API + interface (port 8501) |
| `traffic` | `Dockerfile.traffic` | simulateur de trafic + page de contrôle des pannes (port 8080) |
| `loader` | `Dockerfile.loader` | job unique : charge RCAEval depuis Zenodo vers Aiven depuis le cloud (~12 s par cas), puis éteint |

Les secrets sont saisis comme variables d'environnement dans la console Aiven. Le chargeur (`scripts/load_rcaeval.py`) est reprenable et idempotent.

## Limites connues

- Pannes de type `socket` : 33 % de précision seulement. Les pannes `loss` : 73 %.
- Le live est un simulateur ; le benchmark ne dit rien d'un système de production réel.
- Le chiffre de 45 minutes manuelles est une hypothèse de travail.
- L'historique d'alertes est limité aux 50 dernières ; aucune authentification (démo).
