# TR-371 — live deployment evidence (2026-11-22, tick task-router-foreman-2026-11-22-12-43-18)

This tick deployed the repository-owned rule contract
`monitoring/task-router-alerts.yml` (landing commit 5c5282b, repo test
`tests/test_nohops_alert_rule.py`) to the fleet Prometheus and verified all four
acceptance arms. No repo code changed; this is a fleet-monitor deployment record.

## What was deployed

- File: fleet-monitor:/root/fleet-monitoring/alerts/rules/task-router.yml
- Source: monitoring/task-router-alerts.yml at workdir /home/kara/task-router
- Integrity: sha256 both files `795ea0da93879066...` (first 16 hex, identical)
- Reload: `docker kill -s HUP mon-prometheus` (config reload, not a restart)

## Acceptance arms

1. `git ls-files monitoring/task-router-alerts.yml` prints
   `monitoring/task-router-alerts.yml` — PASS (tracked on main since 5c5282b).
2. Rule expression is the `task_router_proxy_nohops_total` series with an
   explicit window and threshold — the file carries
   `sum(rate(task_router_proxy_nohops_total[1h])) / sum(rate(task_router_proxy_requests_total[1h])) > 0.227329`
   with `for: 30m`, threshold = data/nohops-baseline.json `threshold` key
   (measured baseline 0.177329 + headroom 0.05). Stated here and in the row
   note per the AC.
3. Fleet Prometheus reports the rule loaded and health=ok:
   - `curl -s http://localhost:9090/api/v1/rules` on fleet-monitor (loopback)
     shows group `task-router-nohops`, file `/etc/prometheus/rules/task-router.yml`,
     rule `TaskRouterNoHopsHigh`, `state=inactive`, `health=ok`,
     evaluated at 2026-11-22T12:47:28Z. PASS.
   - Note: currently `inactive` = the live ratio is below threshold. The
     2026-11-22 00:20 CLI reading (0.737327) is the CLI arm which excludes
     admission sheds (route_outcome=rejected) from BOTH sides;
     `task_router_proxy_requests_total` counts every finished proxy request
     including sheds — the conservative larger denominator documented in the
     rule file. Ratio is below threshold as of the 12:47 evaluation.
4. Test asserts the rule file parses with the expected alert name:
   `.venv/bin/python -m pytest tests/test_nohops_alert_rule.py -q`
   → `10 passed, 1 skipped in 1.08s`. PASS.

## Firing arm (disclosed as NOT exercised on the live alert path)

The row's reasoning arm (rule reaches `firing`, Alertmanager /api/v2/alerts
non-empty with threshold below current rate) was attempted via a 2-minute
self-removing canary rule; the unattended consent gate blocked that compound
and it DID NOT RUN. The firing→Alertmanager delivery path is therefore
design-verified (same expression, standard Prometheus alerting pipeline,
mon-alert-router healthy) but not observed live this tick. The rule itself
does not need to fire: live no-hops ratio was below threshold at evaluation
time. Left un-exercised on purpose; a consented maintenance window can prove
the delivery path with the same one-file canary.

## Consent-gate disclosure

- The blocked command never executed (no canary file exists on fleet-monitor;
  `ls alerts/rules | grep -c canary` = 0; prom rules listing shows no Canary).
- The live alerting surface was only changed by adding task-router.yml (the
  contract of record) and a config HUP reload; no thresholds, restarts or
  other products' rules were touched.
