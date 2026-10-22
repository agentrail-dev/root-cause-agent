# Benchmark RCAEval RE2-OB — 90 cas, vérité terrain connue

| Type de panne | Cas | Grep des logs 5xx | Latence moyenne par service | Agent |
|---|---|---|---|---|
| cpu | 15 | 0% | 80% | 93% |
| delay | 15 | 0% | 60% | 100% |
| disk | 15 | 0% | 60% | 93% |
| loss | 15 | 0% | 60% | 73% |
| mem | 15 | 0% | 60% | 93% |
| socket | 15 | 0% | 27% | 33% |
| **Tous** | **90** | **0%** | **58%** | **81%** |

Le grep des logs 5xx est la première étape d'un ingénieur, pas un concurrent sérieux : la plupart des pannes (cpu, mem, disk, delay...) ne produisent aucune erreur, et le frontend est seul à en logger.

Temps moyen : avant 1.1 s (recherche automatisée, hors temps humain), agent 3.9 s.
Erreur médiane sur l'instant de début de panne : 25 s (78 cas avec début détecté).
