# Demo video script — 90 seconds

App: https://01a16335-e6b3-7bfd-b4ef-381f3902c65c-8501.eur-1.aiven.app/
Fault simulator control page: https://01a1637c-3137-7837-bc71-5e1fa8e77400-8080.eur-1.aiven.app/

The live part runs on a traffic simulator in the style of OpenTelemetry, not on the real shop. Say so on camera. The benchmark data is real RCAEval data.

| Time | Screen | Voice-over |
|---|---|---|
| 0:00–0:10 | App dashboard, all services healthy, no open alert | "Production is healthy. Our agent watches it continuously, on three Aiven databases." |
| 0:10–0:25 | Simulator control page: inject **CPU saturation on `checkoutservice`** | "This is a traffic simulator in the style of OpenTelemetry, not the real shop. I saturate the CPU of the checkout service. No error is logged: a grep for 5xx would find nothing." |
| 0:25–0:40 | Back to the dashboard, the checkout service degrades. Speed up or cut the wait. | "The watchdog keeps polling. No one asked the agent anything." |
| 0:40–0:55 | Watchdog alert appears (about 75 s after injection) | "About 75 seconds after the injection, the alert fires on its own." |
| 0:55–1:10 | Alert details: root cause service, evidence, business impact, suggested actions, estimated start | "The agent names the right service, shows its evidence from logs, traces and orders, estimates when it started, and suggests actions." |
| 1:10–1:25 | Benchmark panel: 90 cases, table by fault type | "Now the proof, on real data. 90 annotated failures from RCAEval, nothing sampled. 81 % correct, against 58 % for the best simple baseline, mean latency per service. Socket faults are our weak spot, 33 %. Median start-time error: 25 seconds." |
| 1:25–1:30 | Title card: AgentRail, repo URL | "Root Cause Agent, by AgentRail." |

## Before recording

- Reset to a healthy state first (`python -m scripts.inject_incident --reset`, or the simulator's reset button).
- Do not run the benchmark live: OpenSearch is shared. Show the stored results.
- If the live alert is late, cut the wait in editing and keep the "about 75 s" line honest.
