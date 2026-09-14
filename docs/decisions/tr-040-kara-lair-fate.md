# TR-040 — Fate of orphaned agent `bunker-kara-lair` on bunker-las-02

Status: DECIDED (preserve; reap optional hygiene) · 2026-09-13 · Investigator: worker tick, read-only probes
Board row: TR-040 ("Decide fate of orphaned agent bunker-kara-lair on bunker-las-02")
Related: TR-041 (GAP-070 registry deploy on las-02), QA-TASK-ROUTER-2 (stale QA agents never reaped)

## 1. Context

bunkerd 0.1.3 (pre-GAP-070, no durable registry) unregistered `kara-lair` from daemon memory during the 2026-09-12 tick-38 restart while the agent itself kept running (rootlesskit LISTEN :30000, `bunker-docker-kara-lair.service` active, container `kara-lair-web` Up 2 weeks). Created 2026-08-22 19:35 PDT, ~3 weeks idle, 0 established connections on :30000; the port-pool widening to 10 ranges made the occupancy non-blocking. The board row framed an owner decision: (a) destroy + reap vs (b) preserve/re-register.

**Material update found during investigation:** the premise is stale. On 2026-09-13 las-02 received the GAP-070/TR-041 deploy (binaries 04:24 PDT, `bunkerd.service` restarted 04:25:06 PDT) with `/etc/bunkerd/config.yaml` set to `registry.enabled: true` **and** `reconciliation.mode: adopt`. The startup reconcile re-adopted kara-lair automatically: its record is in the durable registry and `bunker list --server bunker-las-02` now shows it as a tracked running agent. The agent is no longer orphaned; this document records the evidence and closes the decision as preserve-by-default.

## 2. Live evidence (collected 2026-09-13, read-only probes via `ssh bunker2`)

| Item | Finding |
|---|---|
| Tracked status (post-TR-041) | `bunker list --server bunker-las-02` → `kara-lair  running  1% (693.3 MB/64.0 GB)  created 2026-08-23T03:03:12Z` |
| Durable registry | `/var/lib/bunkerd/agents.jsonl`: 21 records; kara-lair `spawn` record written 2026-09-13T11:24:43Z (= 04:24 PDT deploy), `created_at 2026-08-23T03:03:12Z`, ports 30000–30099, limits 4 CPU / 8 GiB / 64 GiB / 16 containers |
| Daemon config (live) | `agent.reconciliation.mode: adopt`, `registry.enabled: true` (path `/var/lib/bunkerd/agents.jsonl`), `max_agents: 8`, pool 30000–30999 @ 100/agent (= 10 ranges) |
| Agent user | `bunker-kara-lair` uid 1001, home `/home/bunker-kara-lair`, shell /bin/bash; systemd linger file present |
| Unit | `bunker-docker-kara-lair.service`: transient, loaded/active/running since Sat 2026-08-22 19:35:49 PDT; plus user session scopes 188 (manager) / 189 (background) |
| Processes (etime) | rootlesskit + slirp4netns + dockerd (`unix:///run/bunker/kara-lair/docker.sock`) + containerd: 21d22h; agent containerd-shim + busybox `httpd -f -p 80 -h /www` + 2× docker-proxy (127.0.0.1:30000, [::1]:30000): 21d22h |
| Container / image | `kara-lair-web` (busybox, `httpd -f -p 80 -h /www`) Up 3 weeks; images: busybox:latest only (6.8 MB); no volumes, no build cache |
| Port 30000 | LISTEN 0.0.0.0 + [::] via docker-proxy; **established connections: 0** (`ss -tn state established "( sport = :30000 )"` → header only) |
| Home dir (295 MB total) | `tj/` 35M · `www/` 8K (`index.html` 3,696 B) · `battery-lab/` 16K (`evil.sh` 55 B, `evil.log`, `marker.txt` — Aug 22 QA battery artifacts) · `.bunker/ports` = `30000-30099` (mtime Aug 22 19:35:50) · `pycode-probe.py`, `rootless-install.sh`, `.ssh/`, `.docker/` — all Aug 22 |
| www/index.html | Single-file dark-theme status card "KARA'S LAIR": clock, uptime, host/agent/port/runtime stats, decorative links; footer "spawned 2026-08-22 · ttl 30d · built by Hermes, for fun". Static demo page, no backend, trivially reproducible. The "ttl 30d" is decorative only — adopted agents carry no durable TTL |
| tj/ git state | Clean worktree on `main`, in sync with `origin/main` of `github.com/totalwindupflightsystems/terminal-jail` (public); **shallow clone (depth 2, `rev-list --count HEAD` = 2)**; 0 unpushed commits, 0 stash entries → zero unique work, fully disposable (re-clone recreates it) |
| Host context | `bunkerd.service` up since 2026-09-13 04:25:06 PDT; 7 *other* `bunker-docker-*.service` units in failed state + ~21 other `bunker-*` linger users (incl. `bunker-tr041-smoke`) — QA-TASK-ROUTER-2 scope, not this row |

## 3. Options

### (a) Destroy + reap
```
ssh bunker2
sudo -n systemctl stop bunker-docker-kara-lair.service
sudo -n systemctl disable bunker-docker-kara-lair.service   # transient unit; stop kills scope 189 + processes
sudo -n loginctl terminate bunker-kara-lair                  # ends user manager session 188
sudo -n rm /var/lib/systemd/linger/bunker-kara-lair
sudo -n userdel -rf bunker-kara-lair                         # frees 295 MB home + port range 30000-30099
sudo -n rm /etc/bunkerd/ssh/kara-lair                        # agent ssh key (registry record removed via daemon destroy path or `bunker registry compact` on-host)
```
Benefit: reclaims 295 MB, one rootless dockerd + container stack, uid 1001, and range 30000–30099; removes one of the stale-agent population counted under QA-TASK-ROUTER-2. Note the original capacity argument is weak now: `max_agents: 8` is the spawn cap and the pool is 10 ranges, so freeing 30000–30099 does not raise capacity above 8.

### (b) Preserve / re-register
The board row's question ("needs GAP-070 deploy, TR-041, or manual re-register") is answered by code + live state:
- **Code**: `internal/agent/reconcile.go` → `Reconcile()` performs startup reconciliation; orphan handling honors `reconciliation.mode` (`internal/config/config.go:234-237`, `destroy` default, `adopt` opt-in, env `BUNKERD_AGENT_RECONCILIATION_MODE`). The re-adopt path is `adoptAgent()` (`internal/agent/reconcile.go:292`): reads the agent's persisted `~/.bunker/ports` via `readPersistedPortRange()` (`internal/agent/registry.go:79`), validates + restores the exact port reservation (`portAlloc.ValidateRange` / `Restore`), rebuilds the tracker record, and persists it to the durable registry. Adoption is exact-port-or-nothing; adopted agents carry no TTL (the reaper never destroys them).
- **No manual RPC/CLI exists**: `bunker registry` exposes `compact` only — adoption happens exclusively at daemon startup reconcile. A manual re-register path would require a code change.
- **Live outcome**: none of that is pending — TR-041 deployed adopt mode and the 2026-09-13 04:25 restart already adopted kara-lair (registry spawn record + `bunker list` membership, §2). Preserve is the current, achieved state.

### (c) Archive-then-destroy
Not applicable: the only candidate artifact (tj/) is a clean shallow depth-2 clone of a public repo with zero unique commits and no stash; www/index.html is 3.7 KB reproducible; battery-lab holds disposable QA artifacts. Nothing unique exists to archive.

## 4. Foreman verdict

**reapable = true** — basis: zero unique data (tj/ is a clean, in-sync, shallow clone of a public repo; www is a reproducible static page; battery-lab is QA residue) and zero traffic (0 established conns on :30000 in 3 weeks). Reapability is about data safety only; see §5 for why reap is nonetheless not the recommended action.

## 5. Recommended action

**Take no destructive action (option b, already in effect).** kara-lair was re-adopted by the TR-041 startup reconcile on 2026-09-13 and is now a first-class registered agent: registry-tracked, exact-port reserved (30000–30099), TTL-exempt. It has run stably for 3 weeks, holds 1% of its disk quota, serves nothing (0 conns), and costs only idle memory. Destroying a healthy, now-managed agent reclaims nothing that blocks anything (pool = 10 ranges vs `max_agents` 8).

If Bane later wants the user + range reclaimed, execute option (a)'s command list verbatim — **gated on explicit Bane sign-off** — as part of the QA-TASK-ROUTER-2 stale-agent sweep rather than as a standalone action.

## 6. Follow-ups

1. **QA-TASK-ROUTER-2** — this host shows 7 failed `bunker-docker-*.service` units and ~21 `bunker-*` linger users (smoke/pool-test agents included). The kara-lair reap, if ever approved, belongs in that sweep.
2. **Bane sign-off gate** — any destroy of kara-lair requires explicit owner approval; this investigation changed nothing on the host (read-only probes only).
3. **Board-row premise** — TR-041 (GAP-070 + adopt mode) landed and resolved the "unregistered on restart" failure mode; the row's destroy-vs-preserve decision is closed as preserve-by-default.
