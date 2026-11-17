# Results

## How this is measured

Anomaly detection is usually demoed with a screenshot of a spiky chart. That
shows nothing — you cannot tell from a chart whether the detector would have
caught the incident you didn't inject, or how often it would have woken someone
at 3am for nothing.

Argus scores itself instead. Every injected fault is written to
`ground_truth.jsonl` with its exact start and end, which turns detection into a
labelled classification problem:

| Term | Definition |
|---|---|
| True positive | an injected fault overlapped by ≥1 alert |
| False negative | an injected fault no alert ever covered |
| False positive | an alert covering no injected fault |
| TTD | seconds from fault onset to the first overlapping alert |

A 45-second grace window is allowed on overlap, since detection legitimately
lags onset — the persistence gate deliberately requires three consecutive
breaching cycles before opening an alert.

Reproduce with:

```bash
make up
make experiment     # standard scenario: 6 faults across 5 types
make report
```

## Scenario

The `standard` scenario alternates faults with quiet periods. The quiet periods
carry as much weight as the faults — they are the only place false positives
can come from, and they include the diurnal traffic peaks that break naive
detectors.

| Phase | Fault | Magnitude | Duration | Quiet after |
|---|---|---|---|---|
| 1 | — (warm-up) | — | — | 180s |
| 2 | `latency_spike` | 1.4 | 150s | 150s |
| 3 | `dependency_degradation` | 1.0 | 180s | 150s |
| 4 | `error_burst` | 1.3 | 120s | 180s |
| 5 | `memory_leak` | 2.0 | 300s | 150s |
| 6 | `partial_outage` | 0.8 | 120s | 150s |
| 7 | `latency_spike` (subtle) | 0.5 | 240s | 180s |

## Measured results

<!-- RESULTS_TABLE -->
| Metric | Value |
|---|---|
| Faults injected | 6 |
| Detected | 6 |
| Missed | 0 |
| False alarms | 0 |
| Precision | 100.0% |
| Recall | 100.0% |
| F1 | 1.000 |
| Median time-to-detect | 28s |
| Mean time-to-detect | 38s |
| False-alarm time | 306s (15.4% of window) |
| Median alert over-hang | 98s |

| Fault type | Injected | Detected | Recall | Median TTD |
|---|---|---|---|---|
| `dependency_degradation` | 1 | 1 | 100% | 28s |
| `error_burst` | 1 | 1 | 100% | 28s |
| `latency_spike` | 2 | 2 | 100% | 28s |
| `memory_leak` | 1 | 1 | 100% | 78s |
| `partial_outage` | 1 | 1 | 100% | 38s |

### Reading these numbers honestly

100% precision and recall looks suspicious, and the first version of it *was*
— see bug 4 below. After fixing the matcher the result holds up, but it should
be read with three caveats:

- **n = 6.** Six faults is a demonstration, not a statistically meaningful
  sample. The `hard` scenario exists to push magnitudes down until things start
  being missed.
- **The faults are synthetic and the service is simulated.** The behaviours are
  modelled on real systems; they are not real systems.
- **Discrete precision/recall flatters any detector with sticky alerts.** The
  number that reflects what an on-call would actually feel is
  **false-alarm time: 306s, 15.4% of the window** — driven by a median
  **98s over-hang**, where alerts stay open after the fault has resolved.
  That is the exit hysteresis (four consecutive cycles under the exit
  threshold) plus signal decay, and it is the most obvious thing to tune next.

Two further tuning findings from the run:

- `error_ratio` sat pinned at the `Z_CAP` of 12.0 for **19% of scored cycles**,
  so severity resolution is being lost at the top end. The cap is doing its job
  of preventing one signal from dominating fusion, but it is set too low —
  ~25 would preserve gradation while still bounding.
- Median TTD of 28s is essentially the persistence gate's floor: three
  consecutive 10s cycles. Detection latency is dominated by the gate, not by
  the statistics, which is the correct tradeoff for alert quality but means
  sub-30s detection needs a shorter evaluation interval.

## What building it actually taught me

Four bugs mattered, and none of them were visible from reading the code. All
four only appeared once real metrics were flowing.

**1. Near-constant signals produce infinite scores.** Pool saturation reads
`0.0` at almost every scrape, because requests usually complete between
scrapes. Median 0, MAD 0. My degenerate-spread fallback multiplied the median
by 0.05 — also zero — so the divisor fell through to a `1e-9` floor and a
perfectly ordinary reading of `0.25` scored **2.5 × 10⁸**, swamping every other
signal in fusion.

The fix is a scale ladder: MAD, then IQR, then an explicit per-signal floor
describing the smallest deviation that is actually meaningful for that metric
(4ms for latency, 0.15pp for error rate, 4MB for heap). Plus a hard cap so no
single signal can dominate without bound. The general lesson: **every metric
needs a declared noise floor**, because "how much variation is normal" is
domain knowledge the data cannot supply on its own.

**2. The baseline was poisoned by its own warm-up.** The first implementation
pre-binned observations into load quantile buckets at `observe()` time. But the
bucket edges are estimated from data that arrives over time — so the earliest
samples were filed against edges that didn't exist yet, and nothing ever
re-binned them. The lowest-load bucket ended up holding peak-hour latencies,
its spread inflated to match, and a genuine 60ms slowdown at low load scored
**1.07** instead of ~6.

A unit test caught this, not the dashboard. The fix was to stop pre-binning
entirely and choose the comparison set at *score* time via k-nearest-neighbours
in load space. Simpler code, no edge artifacts, and `k` became a single tunable
knob:

| k | Slowdown score | Healthy peak score | False breaches |
|---|---|---|---|
| 200 | 1.73 | 1.01 | 0/400 |
| 120 | 3.88 | 0.80 | 0/400 |
| 90 | 6.25 | 0.72 | 0/400 |
| **60** | **8.67** | **0.47** | **0/400** |
| 40 | 10.03 | 0.04 | 0/400 |
| 25 | 12.00 | 0.10 | 1/400 |

k=60 was chosen: strong separation with the healthy peak still well under 1,
and enough samples left that the estimate doesn't get noisy on real data.

**3. Empty vectors are not zero.** `sum(rate(requests{status=~"5.."}[1m]))`
returns an *empty vector* when there have been no errors, not `0`. Dividing by
it yields nothing, the client saw `None`, and the error-rate baseline silently
never filled — which kept the whole detector stuck in warm-up, unable to fire
any alert at all. It presented as "the detector doesn't work" with no error
anywhere in the logs.

`or vector(0)` fixes it. The broader lesson is that **the absence of a metric
and a metric reading zero are different things**, and monitoring code has to
distinguish them deliberately. I also added `argus_baseline_samples` per signal
so the next warm-up stall is diagnosable from the dashboard in ten seconds
rather than by instrumenting the process.

**4. The evaluator scored itself 100% while being broken.** The first scoring
pass returned perfect precision and recall — and a **mean time-to-detect of
minus 30 seconds**, with `partial_outage` at **minus 372 seconds**. An alert
cannot detect a fault that starts six minutes later.

The matcher tested plain interval overlap with a symmetric grace window, so an
alert that had already *closed* 32 seconds before a fault began still counted
as detecting it. Long alerts spanning quiet periods could claim multiple
faults and could never be counted as false positives.

The corrected matcher requires an alert to *open* inside the fault window. The
headline numbers happened to survive — the underlying one-to-one mapping was
real — but they were not evidence of it beforehand. Two changes came out of
this: an assertion that fails the evaluation outright if any TTD is negative,
and the addition of false-alarm *time* alongside discrete counts, because one
alert stuck open for an hour is far worse for an on-call than three short
correct ones.

The general lesson is the uncomfortable one: **a metric that reports success is
not evidence the thing works — the metric needs its own sanity checks.** The
negative TTD was the only reason this was caught at all.

## Honest limitations

- The service is simulated. The behaviours are modelled on real systems, but
  this is not production traffic.
- Conditioning is on a single load variable. Real services vary along several
  (endpoint mix, payload size, cache hit rate).
- No day-of-week or holiday seasonality — only the load coupling.
- Baselines don't persist across detector restarts; a restart re-bootstraps
  from Prometheus, which works, but means the learned state is only as good as
  the retention window.
