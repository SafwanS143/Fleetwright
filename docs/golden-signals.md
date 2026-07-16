# Fleetwright — The Four Golden Signals

The [Google SRE book](https://sre.google/sre-book/monitoring-distributed-systems/) names four
"golden signals" — **latency, traffic, errors, saturation** — as the minimum you should monitor for a
user-facing system. This doc maps each one onto Fleetwright's telemetry pipeline, names the concrete
metric that covers it, and is honest about the one signal that is only partially covered and why.

## The reframe: streaming, not request/response

The golden signals were written for **request-driven** services — a load balancer fronting servers that
answer requests. Fleetwright isn't that shape. It's a **one-way streaming ingest**: devices emit
telemetry at ~10 Hz, the gateway forwards it, the bridge turns it into metrics. There are no user
requests to time.

So the signals don't map one-to-one — they map by **analogy**, and naming the analogy correctly is the
point. "Latency" becomes *delivery* latency (how long a sample takes to show up, and how stale a device
is), not request-response time. Getting this reframe right is what separates reciting the four words
from understanding what they're *for*: catching a problem from the symptom the consumer sees, before you
know the cause.

## The mapping

| Golden signal | What it means here | Metric(s) | Where it lives |
| --- | --- | --- | --- |
| **Latency** | Delivery latency + device staleness — how long between samples, and how old the freshest data is. | `fleet_intermessage_gap_seconds` (histogram → p95 via `histogram_quantile`); `fleet:device_freshness_seconds` (`time() − fleet_last_message_timestamp_seconds`). | Bridge + freshness recording rule |
| **Traffic** | Demand on the pipeline — messages/sec the fleet is pushing through ingest. | `rate(fleet_messages_total[5m])` (per-device and summed); device count online. | Bridge counter |
| **Errors** | Frames that arrived but failed — malformed/undecodable payloads. | `rate(fleet_message_errors_total[5m])`, rolled into `fleet:ingest_error:ratio_rate5m`. | Bridge counter + error-rate SLI |
| **Saturation** | How "full" the pipeline is vs. capacity — the gateway ring buffer, broker, and Pi resources. | **Partial.** `fleet_sequence_gaps_total` is a *downstream symptom* of saturation; direct fill/resource metrics not yet exported. | *(gap — see below)* |

### Latency

Two complementary reads:

- **Inter-message gap** (`fleet_intermessage_gap_seconds`) is a histogram bucketed around the 10 Hz
  (~0.1s) operating point, so `histogram_quantile(0.95, …)` resolves p95 delivery latency exactly where
  it matters. This is the "are samples arriving on cadence?" signal.
- **Freshness** (`fleet:device_freshness_seconds`) is `now − last-seen`. It climbs on its own for a dead
  device because the bridge keeps exporting that device's last timestamp — so it doubles as the liveness
  latency signal and is the basis of the freshness SLO (fresh `< 10s`, Chunk 18).

The Google book stresses tracking the latency of *failed* requests separately from successful ones —
a fast error can hide behind a healthy average. The streaming analog: a device that has died has
*infinite* delivery latency, which freshness captures as an ever-climbing age rather than letting it
vanish from the average. Errors and latency stay separated for the same reason.

### Traffic

`rate(fleet_messages_total[5m])` is the demand signal — how many telemetry messages/sec the fleet is
successfully pushing through ingest, per device and summed. Wrapping the counter in `rate()` is what
makes it a traffic *rate* instead of a meaningless monotonic total. A sudden drop in aggregate traffic
with no corresponding error spike points at devices/gateways going quiet upstream; a climb tracks fleet
growth (this is what the Chunk 26 simulated fleet will exercise).

### Errors

`rate(fleet_message_errors_total[5m])` counts payloads that arrived but couldn't be decoded, tagged by
`reason`. It's rolled into the `fleet:ingest_error:ratio_rate5m` SLI (errors ÷ total frames) with a
`< 0.1%` SLO. Machine-generated NDJSON should essentially always parse, so anything above that points at
framing corruption on the UART/serial hop — a real, localizable fault, not normal load.

**Deliberately *not* in the error signal: packet loss.** `fleet_sequence_gaps_total` (dropped messages,
inferred from firmware `seq` jumps) is tracked on the dashboard but kept out of the error-rate SLI.
Telemetry publishes at **QoS 0**, which trades delivery guarantees for liveness, so a dropped sample in
a 10 Hz stream is *expected*, not a budget-consuming error. Counting it as an error would make normal
operation look broken.

## The honest gap: saturation

**Saturation is the least-covered signal, and that's a deliberate, stated limitation rather than an
oversight.**

Saturation asks "how close to capacity is the most constrained resource?" In this pipeline the
constrained resources are:

- the **gateway ring buffer** (bounded store-and-forward, Chunk 13) — its fill level is the truest
  saturation signal, because a full buffer is the exact moment the pipeline starts *dropping* data;
- the **2 GB Raspberry Pi** — CPU/memory headroom on the gateway;
- the **broker** — connection count and in-flight queue depth.

Today none of these is exported as a first-class saturation gauge. What I *do* have is an **indirect,
downstream proxy**: `fleet_sequence_gaps_total`. When the ring buffer overflows or the link is
backpressured, samples get dropped and the firmware `seq` numbers jump — so a rising seq-gap rate is the
*symptom* of saturation, observed after the fact, not the leading indicator you actually want.

Why it's acceptable *for now*: the honest answer is that at a single-device / small-simulated-fleet
scale the pipeline is nowhere near any of these limits, so a leading saturation signal buys little. The
principled fix is known and staged:

- export **ring-buffer fill fraction** from the gateway as a gauge (the highest-value add — it's the
  resource that directly gates data loss);
- add **cAdvisor / node metrics** for Pi CPU/memory once the stack is containerized on Compose/k3s
  (Chunks 16, 36);
- surface **broker connection/queue** metrics from Mosquitto's `$SYS` topics.

Saying "saturation is my weak signal, here's the proxy I have and here's exactly what I'd add first" is
a stronger interview answer than pretending all four are equally covered — real systems almost always
have one soft signal, and knowing *which* and *why* is the skill.

## One-line summary

Latency, traffic, and errors are covered by first-class metrics wired to SLOs (freshness, ingest rate,
error ratio); **saturation is covered only indirectly** via packet-loss as a downstream symptom, with
ring-buffer fill as the first concrete gap to close.
