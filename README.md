# Argus

**Anomaly detection for service metrics that doesn't page you every morning.**

Argus watches a running service through Prometheus and decides whether its
current behaviour is actually abnormal. The hard part isn't spotting a
catastrophic outage — a static threshold does that. The hard part is spotting a
*subtle* degradation without firing on every legitimate traffic peak. That
tradeoff is the entire project.

It ships with a fault-injection harness, so every claim below is measured
rather than asserted.

---

## The problem

Here is the thing that breaks naive monitoring. In almost any real service,
**latency is coupled to load**:

```
   p95 latency
       │                                    ╭──╮
       │                          ╭─────────╯  ╰──╮        ← evening peak
       │              ╭───────────╯               ╰───╮
       │      ╭───────╯                               ╰──
       └──────┴──────────────────────────────────────────▶
            06:00        12:00        18:00        00:00
```

Every weekday that curve climbs, and it is *completely healthy*. Queueing
theory says so: as concurrency approaches your worker pool size, wait time
grows non-linearly. Nothing is wrong.

So a static threshold (`p95 > 500ms`) fires every evening. And a naive z-score
against recent history fires every morning, because the last hour was quiet and
now it isn't. Teams respond by raising thresholds until alerts stop — at which
point real degradations sail underneath them.

## The approach

Argus doesn't ask *"is latency high?"* It asks:

> **"Is latency high _for this level of load_?"**

It maintains a rolling population of observations bucketed by throughput, then
scores each new sample against the distribution of its own bucket. A traffic
surge moves you into a higher bucket where higher latency is *expected*. A
genuine slowdown makes you an outlier inside whatever bucket you're in.

Three further decisions do most of the remaining work:

| Decision | Why |
|---|---|
| **Median/MAD, not mean/σ** | The anomalies you're hunting would otherwise poison the baseline you score against. One 4-second outlier wrecks a mean; it barely moves a median. |
| **Persistence gate (hysteresis)** | An alert opens only after the score holds above `enter` for N consecutive cycles, and closes only after it sits below a *lower* `exit` threshold. This is the single most effective anti-noise mechanism available, and it costs nothing. |
| **Don't learn during an incident** | Baselines update only when the fused score is below the alert threshold. Otherwise a sustained outage quietly becomes the new normal and the alert resolves itself while the service is still broken. |

Signals are fused with a weighted max plus a corroboration bonus, not a mean —
one signal screaming is the common shape of a real incident, and averaging
would bury it under three calm ones.

## Architecture

```
  ┌──────────────┐   POST /checkout    ┌──────────────┐
  │   loadgen    │────────────────────▶│ checkout-api │
  │ diurnal RPS  │                     │  + faults    │
  └──────────────┘                     └──────┬───────┘
                                              │ /metrics
                                              ▼
  ┌──────────────┐    PromQL           ┌──────────────┐
  │   detector   │◀────────────────────│  Prometheus  │
  │ load-cond.   │                     │  + rules     │
  │ baselines    │────────────────────▶│              │
  └──────┬───────┘   /metrics          └──────┬───────┘
         │                                    │
         │ alerts.jsonl                       ▼
         │ ground_truth.jsonl          ┌──────────────┐
         └────────────────────────────▶│   Grafana    │
                  evaluate.py          └──────────────┘
```

| Service | Role |
|---|---|
| `checkout-api` | FastAPI service with realistic queueing behaviour and five injectable fault modes. Records every injection as ground truth. |
| `loadgen` | Traffic with a two-humped daily shape, burstiness, and a time-compression factor so a simulated day runs in minutes. |
| `prometheus` | Scrape + storage. Also runs the *static-threshold* alert rules the adaptive detector is benchmarked against. |
| `detector` | The actual system: queries Prometheus, scores, gates, alerts. |
| `grafana` | Provisioned dashboard, including a ground-truth overlay so you can see what was really wrong. |

## Quickstart

Requires Docker. On macOS: `brew install colima docker docker-compose && colima start`.

```bash
make up          # build + start everything
make smoke       # ~9 min scripted fault scenario
make evaluate    # score the detector against ground truth
```

Then open **Grafana at http://localhost:3000** (anonymous viewing is enabled).

```bash
make experiment  # ~38 min, six fault types
make hard        # ~26 min, deliberately subtle low-magnitude faults
make logs        # tail the detector
make fault KIND=dependency_degradation MAG=1.2 SECS=180
```

## Evaluation

This is the part most anomaly-detection projects skip, because without labels
you can't answer *"does it work?"* — you can only show a chart with a red line.

Argus generates its own labels. Every injected fault is written to
`ground_truth.jsonl` with its exact window, so detection becomes a scoreable
classification problem:

- **True positive** — an injected fault overlapped by at least one alert
- **False negative** — an injected fault no alert ever covered
- **False positive** — an alert covering no injected fault
- **TTD** — time from fault onset to the first overlapping alert

```bash
make report      # markdown table of precision / recall / F1 / TTD by fault type
```

Results, methodology, and a write-up of the three bugs that only appeared once
real metrics were flowing are in [`docs/RESULTS.md`](docs/RESULTS.md).

```bash
cd services/detector && python3 test_models.py   # 15 tests, no deps
```

## What's deliberately hard about the simulation

A simulator that emits flat traffic plus occasional square waves makes any
detector look brilliant. This one is built to be unkind:

- **Queue-coupled latency.** Service time rises non-linearly with concurrency,
  so traffic surges *look like* slowdowns.
- **Load-induced errors.** Past saturation the service sheds load, so error
  rate and latency move together during healthy peaks too — the same signature
  a real incident produces.
- **A flaky downstream dependency** with its own latency distribution, so some
  incidents originate outside the service.
- **Sawtooth heap with GC**, so a memory leak has to be distinguished from
  normal collection cycles rather than from a flat line.
- **Sub-threshold faults** in the `hard` scenario, at magnitudes low enough
  that a human staring at the dashboard would plausibly miss them.

## Limitations

Worth being straight about:

- **The service is simulated.** The *behaviours* are modelled on real systems,
  but this is not production traffic.
- **Univariate baselines conditioned on one load variable.** Real services have
  several load dimensions (endpoint mix, payload size, cache state). A
  multivariate conditioning approach would be the next step.
- **No seasonality beyond load coupling.** Day-of-week and holiday effects
  aren't modelled; a production version would want an explicit seasonal
  decomposition (STL or similar).
- **Cold start.** Baselines need a warm-up window before they can score
  anything, and the detector reports `argus_baseline_ready=0` until then.
- **Single instance.** No HA, no persistence of learned baselines across
  restarts — a restart re-enters warm-up.

## Repository layout

```
services/checkout-api/   instrumented service + fault injection
services/loadgen/        diurnal traffic generator
services/detector/       models.py (algorithms), detector.py (loop),
                         evaluate.py (scoring)
prometheus/              scrape config + static-threshold baseline rules
grafana/                 provisioned datasource + dashboard
experiments/             scripted fault scenarios
docs/                    measured results and methodology notes
```

## Licence

MIT


## Release review

After a completed, isolated synthetic benchmark, run `make release-review`.
The report recommends either **advance to shadow mode** with paging disabled
or **hold**. It checks the labeled fault count, overall recall, coverage of
each injected fault type, false alarms, median detection delay, and time
spent alerting outside fault windows. A failed gate exits with status 2.

The defaults ask for at least 6 faults, 90% recall, a detection in every
injected fault category, at most 1 false alarm, median detection within
60 seconds, and no more than 180 seconds outside fault windows. The
[evaluator](services/detector/evaluate.py) exposes these thresholds as
flags so a team can set a policy before it runs the benchmark. The
[recorded standard run](docs/RESULTS.md) would hold on alert duration under
this policy even though it detected all six faults.
