# Interview Notes — "Defend it cold"

This file is the actual point of the project. For every chunk, I write — **in my own words** — the
answers to the design questions an interviewer would probe. If the code runs but I can't answer these
cold, the chunk isn't done.

Rule: write the answer _after_ building, from memory, then check it. The gaps you find while writing
are exactly the gaps an interviewer will find.

---

## Chunk 1 — Repo, architecture, the gateway pattern

**Q: Why the gateway pattern? Why not have the device talk to the cloud directly?**

> _(your answer here — the 30-second systems-thinking pitch. Hit: F401RE has no network stack
> (USB/UART/I²C/SPI/GPIO only); a constrained MCU behind a connectivity gateway is the correct design;
> it's exactly how an ECU sits behind a telematics unit in a vehicle.)_

**Q: Give the 30-second end-to-end architecture pitch out loud.**

> _(your answer here — say it out loud until it's smooth, then write the version that came out.)_

--- We need a gateway here because the STM32 F401RE doesn't have a network stack, it only communicates thru SPI/I2C/GPIO/UART. It needs something in between like a Raspberry Pi (in this case) to communicate with it and upload it to the cloud, since the Pi has connectivity. This is very similar to an ECU connecting to a telematics unit.

> Sections below are stubs — fill each in as you complete the chunk.

## Chunk 2 — Pi bring-up

_TCP vs UDP for SSH (why TCP, port 22); what systemd is; systemctl/journalctl; what DNS resolves._

1 - TCP vs UDP:
TCP ensures all packets are sent in order, and resends if missing. This is slower but necessary for ssh commands, compared to UDP which is faster but doesn't have sequencing or resending

2 - What systemd is:
systemd is the init system in the OS of the Raspberry Pi. It controls all the services that run, including SSH. If bugs are encountered, then the logs for systemd will greatly benefit debugging (systemctl - logs for SSH status, and recent connections/journalctl - actual log outputs)

3 - The DNS resolves the issue of remembering the IP of something. For example fleet-gw is easy to remember but 192.193.13.27 is hard. The DNS solves that by tracking and translating IPs to links. The mDNS is the local network version of that, where a device would respond when scanned for a certain name.

## Firmware defends — tiering note

_Reweighted 2026-07-04: Tesla SRE interviews probe systems/reliability reasoning, not firmware
trivia. Chunks 3-9 below are tiered so I know exactly how much to internalize — Interview-critical
gets full depth, One-liner gets a single sentence, and low-signal register/math/formula detail is
either compressed or dropped. Chunks 1-2 are untouched (already right-sized)._

**Tier 1 — Interview-critical, full depth, wherever it lives:**

1. Gateway pattern — Chunk 1 above (constrained MCU with no networking behind a connectivity
   gateway = ECU behind a telematics unit).
2. Heartbeat + sequence numbers — Chunk 9 below (seq# → packet-loss detection; heartbeat →
   telemetry-freshness SLI).
3. Firmware as hop-one of the telemetry troubleshooting chain — Chunk 8 below (is it the sensor,
   the I²C read, or the UART framing that broke?).

## Chunk 3 — Nucleo toolchain

**One-liner tier:**

- What ST-LINK is: a separate on-board debug chip that talks SWD to the F401RE so STM32CubeIDE can
  flash and debug it.
- Bare-metal vs OS: bare-metal means my code owns the whole chip with nothing underneath it; an OS
  would add process scheduling and memory management in between.
- Flashing: goes through the ST-LINK, which unlocks the flash controller, erases what it needs to,
  and writes the binary in.

## Chunk 4 — I²C bring-up

**One-liner tier:**

- What I²C is: a two-wire (SDA/SCL) master/slave bus using 7-bit addressing.
- WHO_AM_I as a part-identity check: my "MPU-6050" breakout returned 0x70, not 0x68 — it's
  a relabeled MPU-6500. Reading the ID register caught a silent BOM substitution before it
  could confuse later debugging (register-map compatible for what we do, so no code impact).

  I2C is a 2 wire communication interface between a master (STM32) and a slave (sensors), talking synchronously thru the SCL signal, with data going thru the SDA signal.

## Chunk 5 — Read the IMU

**One-liner tier:**

- Why configure before reading: the sensor's scale/mode registers have to be set before raw
  readings mean anything.

  The sensor is set to SLEEP mode by default, and the raw values are in terms of g (9.81), so the sensor must be turned on and scaled, as do all sensors after purchase.

## Chunk 6 — BME280 on the same bus

**One-liner tier:**

- Two devices, one I²C bus: they coexist fine since each has its own 7-bit address, no extra wiring
  needed.

  The MPU and BME both live on the same SDA and SCL bus, and the MCU talks to them by specifying an address, 0x68 for the MPU and 0x76 for the BME.

## Chunk 7 — Non-blocking sampling

**One-liner tier:**

- SysTick: a hardware timer interrupt used to drive periodic sampling without blocking the main
  loop.
- Why non-blocking matters: a blocking delay would stall sensor reads and desync sampling from real
  time.

  SysTick is just a timer since the program started. We use that instead of sleep so the MCU can still perform actions like printing, and other UART actions.

## Chunk 8 — JSON-line telemetry over UART

**One-liner tier:**

- UART vs I²C: UART is asynchronous and point-to-point with no shared clock line; I²C is a
  synchronous multi-device bus.

  I2C: A clock and data signal, slaves return data when the master writes to that address on a high clk signal
  UART: No clk signal, one way, and asynchronous, just Rx->Tx from both master and slave.

**Interview-critical tier:**

- Why newline-delimited JSON, and why this is hop-one of the troubleshooting chain: if telemetry
  stops, the newline-delimited framing is the first thing that tells me whether the fault is the
  sensor, the I²C read, or UART framing itself — a corrupted/missing delimiter points at UART, a
  bad value points upstream at the read or the sensor.

  NDJSON is just like normal JSON but each JSON object is seperated by a new line. This makes it easy to tell where one message starts and the other ends, making debugging easier, as well as finding which points of the system have problems based on values/messages.

## Chunk 9 — Heartbeat + sequence numbers

_Why seq# (packet-loss detection); why heartbeat (basis of the freshness SLI, liveness ≠ payload)._

The seq number detects any packet loss, as all the JSON objects should be numbered in order, and if any go missing then it's clear.

The heartbeat signal every 5s is very similar to a liveness probe, it makes sure that the MCU is alive and the connection between the MCU and the gateway is working, narrowing telemetry problems to the sensors and firmware drivers. The payload (the sensor data) isn't relied on for the heartbeat signal.

---

# Phase 2 — Gateway (MQTT is the key new skill)

_The Pi stops being "the thing SSH runs on" and becomes the connectivity gateway: it reads the
Nucleo's serial stream, republishes it over MQTT, survives broker/device outages, and ships as a
container. Interview weight sits on MQTT (pub/sub, QoS, retained, LWT) and store-and-forward._

**Tier 1 — Interview-critical, full depth:**

1. Gateway-parse hop — Chunk 10 (partial-line buffering until newline; the second hop after
   firmware in the troubleshooting chain).
2. Pub/sub + the broker's job — Chunk 11 (decouples producers from consumers).
3. QoS / retained / LWT + why MQTT over HTTP — Chunk 12.
4. Store-and-forward — Chunk 13 (in-flight messages on a drop; why the buffer is _bounded_).
5. Serial device passthrough into the container — Chunk 14.

## Chunk 10 — Pi reads serial

**One-liner tier:**

- How a Linux process gets at the device: it opens the character device the kernel exposes for the
  USB-serial adapter — `/dev/ttyACM0`.

  The kernel talks to IO devices thru files. It sets a file called `/dev/ttyACM0`, and that's what allows reading from the Pi. A character device just means a device that sends a stream of data.

**Interview-critical tier:**

- What happens on a partial line and how you buffer until newline — this is the gateway-parse hop in
  the troubleshooting chain. A read can return half a JSON line (or one-and-a-half); you accumulate
  bytes and only parse a record once you've seen the newline delimiter, holding the remainder for the
  next read. Also: reconnect cleanly if the Nucleo is unplugged/replugged.

  The Pi detects new objects via the newline. If there's a partial line, that's normal serial communication. It keeps buffering until you detect a \n, in which it processes the line as a complete JSON object. The rest will stay in the buffer for the next object. This also helps with debugging and narrowing down problems in case of unexpected JSON outputs.

## Chunk 11 — Mosquitto broker + first publish

**Interview-critical tier:**

- The pub/sub model and what a broker is _for_: producers publish to topics, consumers subscribe;
  neither knows about the other. The broker decouples them — the gateway doesn't need to know who
  (or how many) is consuming, and consumers can come and go without the gateway changing.

  Publish: Device give data to the broker
  Subscribe: Device/service allows receiving of messages from a broker

  The pub/sub model is a model that allows for data to be transmitted from a device to device/service thru a broker, via messages that fall under topics. The broker is essentially a middleman that accepts messages from publishers and distributes messages to subscribers. This is beneficial so you can decouple the device giving data (Pi) from the services accepting data. This is useful because without it, each service would need it's own stream of data set up, which gets messy, and requires the Pi to know all the services which require the data and would have to deal with failures regarding said connections. This abstracts that whole layer to a middleman with conviniency using topics.

**One-liner tier:**

- Topic design and why hierarchical: topics like `fleet/<id>/telemetry` — the hierarchy lets a
  subscriber wildcard across the fleet (`fleet/+/telemetry`) or narrow to one device.

  This makes it easy to scale horizontally and add more devices, getting messages from a certain device, as well as allowing data from all devices in a certain subtopic, like telemetry

## Chunk 12 — MQTT depth: QoS, retained, last-will

**Interview-critical tier:**

- QoS 0/1/2 delivery guarantees and which you chose + why:
  - QoS 0 — at most once (fire-and-forget, may be lost).
  - QoS 1 — at least once (acked, may duplicate).
  - QoS 2 — exactly once (four-way handshake, slowest).
    Say which you picked for telemetry and the tradeoff behind it.

  I picked QoS 0 for telemetry. There are several reasons for this, first being that telemetry fires at 10Hz, meaning ACKs and four way would quickly clutter the broker and Pi connection. Resending messages hundreds of milliseconds afterwards has no benefit, since the temperature at that instant has no significance for a 10Hz stream. We have a seq # that we rely on for completeness, and the SLO is for liveness, not completeness (missing a tenth of a second doesn't matter for temp/accel etc.)

- Retained messages: the broker keeps the last message on a topic so a _new_ subscriber gets current
  status immediately instead of waiting for the next publish.

  A retained message is a single message that is saved on the broker level, one for each topic. Subscribers immediately get the retained message upon subscribing, allowing for offline statusses to show. This is really helpful for when the gateway (Pi) publishing connecting is degraded.

- Last-Will-and-Testament: a message the broker publishes on the gateway's behalf if the connection
  drops uncleanly — so a dead gateway marks its device offline without any live code running.

  The LWT is a message that the broker publishes when it detects a degraded gateway that's been stopped abruptly. It lets the subscribers know that there's an issue with the gateway instead of just the gateway not sending data. It is a retained message on the /status topic and for abrupt internet disconnections and program ungraceful stops, it waits for 1.5 \* keepalive time before it published the LWT message. For program kills, it sends the message immediately.

- Why MQTT for fleet telemetry instead of plain HTTP.

  MQTT allows for decoupling between the publishers and subscribers.

  Plain HTTP means you do 10 POST and GET per second, constantly polling, which is costly and not nearly as efficient as real time streaming.

  HTTP is client initiated, which requires the endpoint's ip/NAT, which gets messy. MQTT's connection comes with build in features like LWT, retained and QoS, which makes it a much better choice.

## Chunk 13 — Ring buffer / store-and-forward

**Interview-critical tier:**

- What happens to in-flight messages when the gateway drops, and how the buffer covers it: when the
  broker is unreachable, telemetry is buffered locally and flushed on reconnect so nothing is lost up
  to buffer size.

  When the gateway between the Pi and the broker drops, the messages sent to the broker will return an MQTT error. When this happens, the messages are thrown into our ring buffer, which is bounded and stores the data until the connection is regained, in which it will publish data at 500 messages/sec, while also parsing the incoming 10/sec

- Why _bounded_: an unbounded buffer would grow until the 2GB Pi runs out of memory during a long
  outage. A bounded ring buffer applies backpressure / drops oldest — memory safety over completeness.

  If it was unbounded, a few hours of shortage would kill the RAM of the 2GB Pi. We keep it bounded to 6000 messages (5 mins), which is good enough for minor breakdowns which would require an SSH into the Pi to restart musquitto. Once max capacity of the buffer is reached, it starts removing the oldest (least important) messages first, hence called a ring buffer.

## Chunk 14 — Containerize the gateway

**One-liner tier:**

- What a container actually is (namespaces + cgroups) and how it differs from a VM: shared kernel,
  isolated view of the system — not a full guest OS like a VM.

  A container is a running image (a snapshot of your program and it's environment and dependencies). It has properties like namespaces (controls what a process can see, like other processes, users, etc.) and cgroups (what a process can use, ie. device data). It's much more efficient than a VM since it shares the same OS and you allocate the resources that the container itself needs rather than a whole OS running on top of yours.

- When you'd run a plain `systemd` service instead of a container.

  If you have a simple process that you want constantly run and handled by the OS of the Pi, then you'd run a systemd service. In our case, we had reads from other devices, which requires access to the /dev port on the Pi, which is better on a container.

**Interview-critical tier:**

- How the container reaches the serial device (device passthrough): `/dev/ttyACM0` has to be passed
  into the container explicitly (e.g. `--device`), because a container doesn't see host devices by
  default. `FLEET_SERIAL_PORT` env var selects which port the reader opens.

  A container doesn't see host machine services by default. They must be allowed to see said passthrough (/dev folder) by the cgroup and mounting the /dev folder. The serial port is also added to the env variables instead of passing on explicit file run commands, since it's now a docker container service and not a file being run manually. This is a better tradeoff for this scale of project to allow the container to see the device driver regardless of when the plug was inserted

---

# Phase 3 — Cloud observe (Prometheus + Grafana)

_Mostly reused from the SRE Monitor, so the build is fast — the weight is entirely on defend.
Interviewers probe whether I actually understand the pieces I reused: metric types, the pull model,
`rate()`, histogram quantiles, and the SLI/SLO/error-budget vocabulary. This is where "deployed
Prometheus" has to become "can reason about it."_

**Tier 1 — Interview-critical, full depth:**

1. Metric types + the pull model — Chunk 15 (counter vs gauge vs histogram; why Prometheus scrapes instead of receiving pushes).
2. `rate()` on a counter — Chunk 16 (why a raw counter is meaningless without it).
3. p95 out of a histogram — Chunk 17 (buckets → quantile estimate).
4. SLI / SLO / SLA + error budgets — Chunk 18 (how each number was chosen; symptom-based alerting).
5. Four golden signals mapping — Chunk 19 (which channel covers which signal, and the honest gap).

## Chunk 15 — MQTT→Prometheus bridge

**Interview-critical tier:**

- Counter vs gauge vs histogram, and which you used where and why:
  - Counter — monotonically increasing (messages, errors); only goes up (or resets to 0).
  - Gauge — a value that goes up and down (last temp, last-seen age).
  - Histogram — samples bucketed into ranges (inter-message latency), the basis for quantiles.

  A counter is just a number that gets incremented or reset to 0. We used it for the total messages and errors, since on a restart, we'd want those values to go back to 0. We also wouldn't graph these values, only their most recent value has meaning

  A guage is a value that goes up and down. This is something you would graph, like legit all of our sensor data. This is because going up and down needs to be permitted.

  A Histogram is a special metric type that splits data into buckets and counts numbers for each. We use this to actually find out p95 latency of our messages later on.

- Why Prometheus **pulls (scrapes)** `/metrics` instead of receiving pushes, and the tradeoff.

  Making Prometheus pull instead of receiving pushes gives us a target health signal and removes the need for the target to push time, since Prometheus scrapes on it's own time. The only problems are that Prometheus needs to be able to reach the target (NATs and firewalls can get in the way).

**One-liner tier:**

- What an exporter is: a process that exposes metrics on `/metrics` in the format Prometheus scrapes.

  An exporter is a process that converts the raw metrics into a new /metrics endpoint that Prometheus updates every specified n seconds.

## Chunk 16 — Prometheus + Grafana via Docker Compose

**One-liner tier:**

- The scrape config and scrape interval: how Prometheus is told what target to hit and how often.

  Scrape config is a list of jobs (endpoints). The configs have the scrape intervals (as well as a global interval) which is how often data is scraped from the endpoint.

**Interview-critical tier:**

- What `rate()` does and why you wrap a counter in it: a raw counter's absolute value is meaningless;
  `rate()` gives the per-second increase over a window, which is the thing you actually graph/alert on.

  A raw counter doesn't mean anything. Having a number of how many messages a certain device sent doesn't provide value, but the rate of which the messages come in is a meaningful data to graph. Graphing raw counters is almost never useful. Guages on the other hand go up and down, and in most cases are useful to graph (like our temp/pressure)

## Chunk 17 — Per-device dashboard

**Interview-critical tier:**

- How you get **p95 out of a histogram** (buckets → quantile estimate): `histogram_quantile()` over the
  bucket counts estimates the value below which 95% of samples fall — an interpolation across buckets,
  not an exact percentile.

  We can't directly calculate the p95 latency. In fact we don't even store how long each message took to reach. We put the latency of messages into buckets as counters and we store the counters themselves. We estimate the 95th percentile across those buckets, assuming an even spread. It's not a perfect number but gets the job done.

**One-liner tier:**

- What freshness looks like as a metric: `now − last-seen` (time since the last message from a device).

  The freshness is the time since the last message was sent from the device. This is very useful to diagnose connections between the broker, device, and device health

## Chunk 18 — SLIs / SLOs / error budgets

**Interview-critical tier:**

- SLI vs SLO vs SLA: the measured indicator, the target you set for it, the contract with consequences.

  SLO: Service level objective. This is a goal that you set while making the project to ensure user experience

  SLI: The actual measured indicator. This should almost always be within your SLOs. SLOs are the goal, SLIs are the actual values the goal wants to stay within

  SLA: Service level Agreement. This is between the client and the provider of the service, entailing SLOs and consequences for breaching the SLOs

- How you chose each number (freshness / availability / error rate) — a reason, not a vibe.

  Freshness (10s): Any tighter than 10s would mean that non-errors would be alerted for, any greater would mean a bad MTTD for devices that are actually down

  Availability (>= 95%): Same signal, but this is across devices. Any tighter would require devices being on a very constrained budget. Looser would mean devices aren't reliable.

  Error rate (< 0.1%): This one's very tight since it doesn't include packet loss up from the gateway to the broker. This should be very low as this would mean a parsing error or data that was transmitted failing to be processed

- What an **error budget** is and what it lets you _do_ (ship vs. freeze).

  An error budget is the leeway allowed by your SLOs. This lets you measure downwtime and decide whether you should ship the product and make changes within the budget, or freeze the service.

  An example of this would be 99.5% uptime over 30 days. The error budget is the number of hrs you can be down.

- Why you alert on **symptoms / SLO burn**, not every raw cause.

  Alert for symptoms, since they actually account for the SLO budget burning and are what the client's directly impacted by. Raw causes are great for logs to then diagnose what happened, but symptoms give the whole picture and whether or not it's a problem.

## Chunk 19 — Four golden signals mapping

**Interview-critical tier:**

- Which telemetry channel covers which signal (latency / traffic / errors / saturation), and which
  signal you're **not** covering and why — be honest about the gap.

  Latency: Redefined as delivery latency, since our service has no requests. This is covered by the fleet freshness signal fleet_intermessage_gap_seconds

  Traffic: rate(fleet_messages_total[5m]). Pretty simple

  Errors: rate(fleet_message_errors_total[5m]). This only covers errors in parsing etc. after the gateway stage. It doesn't catch anything during the firmware/TCP connection to the Pi. It also excludes gaps in seq since we trade completeness for liveness by using QoS 0 in our publish.

  There is no saturation, since we don't really have a queue for requests or anything. The ring buffer is for sending messages while the broker is down, and removes messages immediately. Nothing about crashing systems here.

# Phase 4 — Reliability (adapt the SRE Monitor — first postable state)

_Mostly adapted from the SRE Monitor (Isolation Forest, Slack alerting + dedup, SQLite incidents), so
like Phase 3 the build is fast and the weight is on defend. Interviewers probe ML claims hardest, so
Chunks 20–21 (why per-channel, IF limitations) are the highest-risk answers here. The rest is SRE
vocabulary — symptom-based alerting, alert fatigue, MTTD vs MTTR, runbooks — that has to be reflexive._

**Tier 1 — Interview-critical, full depth:**

1. Why **per-channel** IF models, not one global model; why unsupervised fits — Chunk 20.
2. IF **limitations** + contamination/threshold tradeoff; IF vs z-score, and when _not_ to use IF — Chunk 21.
3. Why **severity + routing**, and why symptom/SLO-based firing beats cause-based — Chunk 22.
4. How **dedup + suppression** stop flapping, and why the window is the length it is — Chunk 23.
5. **MTTD vs MTTR** and which the incident design optimizes — Chunk 24.
6. Walk the **architecture diagram** end to end — Chunk 25.

## Chunk 20 — Isolation Forest per channel

**Interview-critical tier:**

- Why **per-channel** models, not one global model; and why an unsupervised method fits here (no
  labeled fault data, "normal" is all you can define).

  _(your answer here)_

**One-liner tier:**

- How IF works at a high level: random splits partition the data; a point that isolates in a **short
  path** (few splits) is easy to separate and therefore anomalous.

  _(your answer here)_

## Chunk 21 — IF limitations + threshold tuning

**Interview-critical tier:**

- The **limitations**: false positives when the device legitimately changes regime (a BME280 actually
  heating up is "anomalous" but not a fault). Name one concrete false-positive scenario and your
  mitigation.

  _(your answer here)_

- How **contamination / threshold** trades false positives vs. missed detections.

  _(your answer here)_

- Why you'd pick IF over a plain **z-score / static threshold** — **and when you wouldn't.**

  _(your answer here)_

## Chunk 22 — Alerting: severity + routing

**Interview-critical tier:**

- Why **severity + routing** matters (getting the right signal to the right owner, not one undifferentiated firehose).

  _(your answer here)_

- Why **symptom / SLO-based** firing beats cause-based firing.

  _(your answer here)_

## Chunk 23 — Dedup + suppression

**Interview-critical tier:**

- How the design stops **flapping** (dedup + a suppression window), and **why the window is the length
  it is** — the tradeoff behind the number.

  _(your answer here)_

**One-liner tier:**

- What **alert fatigue** is, and why a noisy alert is worse than no alert.

  _(your answer here)_

## Chunk 24 — Incident store + timeline

**Interview-critical tier:**

- **MTTD vs MTTR** — what each measures, and which your design optimizes.

  _(your answer here)_

**One-liner tier:**

- The **incident lifecycle**: open on trip, close on recovery, timeline of what happened in between.

  _(your answer here)_

## Chunk 25 — Runbooks + finalize architecture diagram

**Interview-critical tier:**

- Walk the **architecture diagram** end to end — every hop, and what fails at each.

  _(your answer here)_

**One-liner tier:**

- What a **runbook** is for, and how it cuts MTTR.

  _(your answer here)_

<!-- Phases 5-7 stubs added as you reach them. -->
