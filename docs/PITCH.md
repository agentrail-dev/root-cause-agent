# Root Cause Agent — 3-minute pitch (AgentRail)

Sources for every number: `results/benchmark.md` and `COORDINATION.md`. Nothing else is quoted.

## 1. The customer problem (0:00–0:30)

An SRE is paged at 3 a.m. Something is slow, nothing obviously errors. They jump between a log tool, a tracing tool and the business database to work out *which service* is at fault and *who is hurting*.

- Our working assumption is that this takes about **45 minutes** per incident. That figure is an assumption for the pitch, not a measurement.
- Grepping the logs for 5xx errors, the usual first step, finds nothing in **74 of 90** real failures: most faults (cpu, mem, disk, delay) produce no error at all.

## 2. What the agent does (0:30–1:15)

A LangGraph agent that reasons in several steps and across several sources:

`analyze_question → detect_anomaly → find_faulty_span → check_business_impact → synthesize`

- Reads logs, traces and orders, then writes the root cause, the business impact and suggested actions.
- A watchdog polls service health continuously. A new incident triggers the agent on its own, and the alert is stored in PostgreSQL.

## 3. Data at the heart of it (1:15–1:50)

Three Aiven services, each with a distinct job:

| Aiven service | Role |
|---|---|
| ClickHouse | logs: 15,053,223 rows |
| OpenSearch | traces: 34,461,235 spans |
| PostgreSQL | orders (business impact) and persistent watchdog alerts |

All 90 RCAEval RE2-OB failures are loaded. Nothing is sampled.

## 4. Proof (1:50–2:30)

Benchmark on **90 real, annotated failures** (RCAEval RE2-OB, ground truth known, run without an LLM so it is reproducible):

- **81 % (73/90)** correct faulty service, against **58 %** for the serious baseline "highest mean latency per service". Grepping 5xx logs (0 %) is only an engineer's naive first step, not a competitor: 74 of the 90 cases log no error at all.
- By fault type: delay 100 %, cpu / disk / mem 93 %, loss 73 %, socket 33 %. We say openly that socket is our weak spot.
- Median error on the failure start time: **25 s** (78 cases with a detected start).
- Agent diagnosis time: 3.9 s on average.
- Live: in the run we measured, a fault injected on the simulator produced a watchdog alert in **about 75 s**, with the right service and a start time within **19 s** (a single run, not an average).

Honesty note: the **live demo runs on a traffic simulator in the style of OpenTelemetry**, not on the real shop. The **benchmark data is real RCAEval data**.

## 5. Execution (2:30–3:00)

- Deployed on Aiven Runtime, public URL, public repo.
- Continuous watchdog with persistent alerts, a dashboard, a benchmark endpoint, post-mortem export.
- Reproducible: `python -m scripts.run_benchmark` regenerates `results/`.

Close: *"From a page at 3 a.m. to a named root cause, with evidence, in seconds."*
