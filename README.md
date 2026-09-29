# QCluster

Run language models that are **too big for one Arduino UNO Q** by pooling the RAM of
several boards connected over ADB.

A host UNO Q (4 GB) discovers every child board plugged into its USB hub, stages a
`llama.cpp` runtime onto each of them, and starts a single `llama-server` that spreads
the model's layers across all boards using llama.cpp's **GGML RPC backend**. The result
is one OpenAI-compatible endpoint backed by the combined memory of the cluster.

Every board shows its own live CPU and RAM utilisation on its 8×13 LED matrix.

```
                 ┌──────────── HOST UNO Q 4GB ────────────┐
 browser :7000 ──┤ qclusterd (stdlib python, no root)     │
 OpenAI clients  │   web UI + /api + /v1 proxy            │
                 │   adb discovery / provisioning         │
                 │   llama-server 127.0.0.1:8081          │
                 │ q-cluster-display app → LED matrix     │
                 └───────────┬───────────────┬────────────┘
                adb forward  │               │  adb forward
          127.0.0.1:5101→50052               127.0.0.1:5102→50052
                 ┌───────────▼────┐    ┌─────▼──────────┐
                 │ CHILD UNO Q 2GB│    │ CHILD UNO Q 2GB│
                 │ rpc-server     │    │ rpc-server     │
                 │ display app    │    │ display app    │
                 └────────────────┘    └────────────────┘
```

## Project at a glance

QCluster is an advanced, experimental project for running a local LLM when model
memory matters more than response speed. It has been tested with:

- one 4 GB UNO Q host and two 2 GB UNO Q children for pooled 3B and 7B models;
- one host plus 15 children for Qwen2.5-32B-Instruct Q3_K_S; and
- a powered USB hub, USB data cables, and a network connection on the host board.

It is not a shortcut to fast inference. Pooling increases the size of model that can
fit in RAM; it adds USB/RPC latency at every layer boundary. See
[Performance expectations](#performance-expectations) and
[Measured results](#measured-results) before choosing hardware.

The cluster itself needs neither Wi-Fi nor a router: ADB over USB provides shell
access, file transfer, and the loopback network paths between the host and children.
Only the host needs network access when downloading models or serving the dashboard
to another device.

## Project videos

- [How QCluster runs a 32B-parameter model on UNO Q boards](https://youtube.com/shorts/uDDLjvYg2iI?feature=share)
- [Measuring approximately 1 W per board during inference](https://youtube.com/shorts/2z0fbQdFPHI?feature=share)
- [Switching a cluster board back to Workshop Mode](https://youtube.com/shorts/On5ZFprrvvk?feature=share)

## Why this shape

Three constraints on a factory UNO Q image drove the design:

| Constraint | Consequence |
|---|---|
| `sudo` is password-gated | Everything runs unprivileged. `adb` is extracted from `.deb` packages into a user-local prefix; `llama.cpp` is compiled in a Debian container via the `docker` group. Only USB device permissions need a one-time root step. |
| No compiler, no web framework, no `pip` packages | The daemon is **pure standard library**. Live updates use Server-Sent Events instead of WebSockets, which also matches how `llama-server` streams tokens. |
| Arduino apps run in bridge-networked containers that cannot see `/home/arduino/qcluster` or the host loopback | Orchestration lives in a host daemon. The on-board app is a thin, identical, config-free LED display on every board. `/proc` and `/sys/class/thermal` *are* host-wide inside the container, so the display app is fully self-sufficient. |

Child boards are reached entirely through ADB — telemetry via `adb shell`, the RPC port
via `adb forward`. Nothing on a child listens on a network interface: `rpc-server` binds
`127.0.0.1` only, which is exactly what the llama.cpp documentation requires, since the
RPC protocol is unauthenticated.

## Layout

```
qcluster/
├── daemon/
│   ├── qclusterd/          host daemon (stdlib only)
│   │   ├── adb.py          safe argv-only adb wrapper
│   │   ├── nodes.py        discovery, stable slot map, telemetry
│   │   ├── provision.py    manifest-diffed runtime push, rpc-server lifecycle
│   │   ├── models.py       allowlisted catalog + resumable downloader
│   │   ├── engine.py       llama-server supervision + placement planning
│   │   └── gateway.py      web UI, control API, OpenAI-compatible proxy
│   ├── models.json         model allowlist
│   └── web/                single-page dashboard (no build step)
├── apps/q-cluster-display/ Arduino app deployed to every board
├── shared/qcluster_common/ LED rendering + /proc sampling, used by both sides
└── scripts/                build, deploy, one-time USB permission setup
```

## Hardware and software

| Item | Required | Notes |
|---|---:|---|
| Arduino UNO Q (4 GB) | 1 | Host board; runs the dashboard and `llama-server`. |
| Arduino UNO Q (2 GB or 4 GB) | 2–15 | Child boards; each runs `rpc-server`. More usable RAM gives the cluster more capacity. |
| Powered USB hub | 1 | Connects the child boards to the host. |
| USB data cables | 1 per child | The ADB connection carries provisioning, telemetry, and RPC traffic. |
| Host network connection | 1 | Needed to download models and access the dashboard. Children intentionally do not need network access. |
| Linux, Docker access, Git, and Python 3 | host environment | Used to build and operate the bundled runtime. |

Start with a host and two children. The [3B results](#measured-results) demonstrate
the basic pooled-memory configuration; use more boards only when the model-size goal
justifies the slower pipeline.

## Setup

### 1. One-time root step (USB permissions)

```bash
sudo ./scripts/setup-usb-permissions.sh
```

This is the only thing in QCluster that needs root. It installs a udev rule for
Arduino's USB vendor id so `adb` can claim the child boards. Everything else runs
unprivileged.

### 2. Install adb (no root)

```bash
./scripts/fetch-adb.sh
```

Downloads the Debian `adb` packages and unpacks them under `tools/`, then generates a
wrapper that points the dynamic linker at them.

### 3. Build the llama.cpp runtime

```bash
git clone --depth 1 https://github.com/ggml-org/llama.cpp build/llama.cpp
JOBS=3 ./scripts/build-llama.sh
```

Builds inside a container with `-DGGML_RPC=ON` and stages `llama-server`, `llama-cli`,
`rpc-server` and the shared libraries into `runtime/`, alongside a `manifest.json` of
sha256 hashes that the provisioner uses to avoid re-pushing unchanged files.

### 4. Deploy the LED display app

```bash
./scripts/deploy.sh --install
arduino-app-cli app start ~/ArduinoApps/q-cluster-display
```

Child boards get the same app pushed and started automatically during provisioning.

### 5. Run the daemon

```bash
./scripts/install-startup.sh
```

The installer enables the `qclusterd` systemd user service and checks lingering, so
the host daemon starts at boot before anyone logs in. If lingering is not already
enabled on a fresh board, run `sudo loginctl enable-linger arduino` once and rerun
the installer.

Or run the daemon in the foreground for debugging: `PYTHONPATH=daemon python3 -m qclusterd`

Open `http://<host-board-ip>:7000`.

## Using it

- **Cluster** — every board with live CPU, RAM, temperature and RPC state; pooled RAM
  total; Identify, reprovision and RPC restart controls; the live `llama-server` log.
- **Models** — catalog with a fit indicator (*fits host alone* / *needs pooling* /
  *will not fit*), resumable downloads, load/unload, and a form to add any
  Hugging Face `.gguf` link.
- **Chat** — streaming chatbox with system prompt and sampler controls, showing
  time-to-first-token and total latency, plus a **Clear** button that empties the
  conversation context.
- **API** — the base URL, current model name, and copy-paste `curl` / `openai` /
  `requests` snippets.
- **Settings** — host WiFi, board shell passwords, physical board identification, and
  decommissioning.

Any OpenAI client works:

```python
from openai import OpenAI
client = OpenAI(base_url="http://<host-board-ip>:7000/v1", api_key="not-needed")
```

### Identifying a physical board

Press **Identify** on a board card in either Cluster or Settings. QCluster blinks that
board's MPU-controlled user LED: the host uses a long pulse, while a child blinks its
slot number in quick pulses, repeated twice. Previous LED brightness and trigger state
are restored afterward. Firmware images that expose no writable user LED show the
control disabled rather than attempting an unsafe fallback.

### Adding your own model

Paste a direct Hugging Face link to a `.gguf` file into the Models tab — both
`/blob/` and `/resolve/` URLs work. The file size is read from Hugging Face rather
than trusted from the browser, and the RAM estimate is derived from it so the fit
indicator stays meaningful.

### Host WiFi

Only the host board needs a network: it serves the UI and downloads models. Child
boards are reached exclusively over USB/ADB and are deliberately kept off the
network, which is also why nothing on them listens on a network interface.

The Settings tab shows the host's interface, SSID, IP and signal, and can scan for
and join a network. This works unprivileged because the board's user is in the
`netdev` group.

### Board shell passwords

QCluster runs unprivileged, so this is only needed for administrative jobs that
genuinely require root — currently just installing the USB udev rule on the host.

For each board you can save an existing sudo password (verified against the board
before it is stored) or set one on a board that has none. Passwords are checked with
`sudo -k` so a cached sudo timestamp cannot make an incorrect password look valid,
are stored in `state.json` with `0600` permissions, are passed to `sudo` on **stdin**
rather than argv or the environment, and are never logged or returned by the API.

If you would rather not store a board password at all, skip this and run
`scripts/setup-usb-permissions.sh` by hand — nothing else needs root.

### Returning a board to its original state

**Settings → Decommission** stops `rpc-server`, deletes `~/qcluster` from the board,
removes the ADB port forward and — if you choose — uninstalls the QCluster Display
app. The board is then left alone: the supervisor stops managing it and the decision
survives daemon restarts. **Re-enable** provisions it again from scratch.

Those two paths are everything QCluster ever writes to a child board, so a
decommissioned board is back to how it started.

## Measured results

Three boards: one UNO Q 4 GB host plus two UNO Q 2 GB children on a powered USB hub.

| | SmolLM2-360M Q4 | Qwen2.5-1.5B Q4 | Qwen2.5-3B Q4 | Mistral 7B Q4 |
|---|---|---|---|---|
| Model file | 259 MB | 986 MB | 1930 MB | 4370 MB |
| Fits the host board alone? | yes | **no** | **no** | **no** |
| Boards used | 3 | 3 | 3 | 5 |
| RSS on host `llama-server` | — | 211 MB | 80 MB | 42 MB |
| Largest child `rpc-server` RSS | 215 MB | 547 MB | 1067 MB | 2179 MB |
| Generation | 8.1 tok/s | 2.8 tok/s | 1.6 tok/s | 0.31 tok/s |
| Prompt processing | 16.6 tok/s | 5.2 tok/s | — | 0.81 tok/s |

The 3B column is the point of the project: a model that needs more RAM than any
single board has free runs because nearly all of it lives on the two children, while
the host holds only 80 MB. Swap was barely touched (27 MB and 82 MB), so the
placement is tight but not reckless. The cost is throughput, exactly as expected from
pipeline-parallel RPC.

With four child boards attached (one 4 GB and three 2 GB), Mistral 7B Q4 loads and
answers through the OpenAI-compatible API. It is usable as a proof of pooled memory,
but not interactive: a 70-token response took just over four minutes.

### Sixteen boards: Qwen2.5-32B Q3_K_S

Sixteen boards (1 host + 15 UNO Q children) pool about 17.9 GB of usable RAM. That's
enough to run **Qwen2.5-32B-Instruct** (Q3_K_S, 13.7 GB file) — a model an order of
magnitude past what any single board could ever hold, and requiring nearly the whole
cluster to fit.

| | Qwen2.5-32B Q3_K_S |
|---|---|
| Model file | 13.7 GB |
| Boards used | 16 (1 host + 15 children) |
| Fits the host board alone? | **no** |
| Cold load (first time, streaming to all 15 children) | ~48 min |
| Reload (`rpc-server --cache` hit) | ~8 min |
| Prompt processing | 0.16 tok/s |
| Generation | 0.13 tok/s |

`--tensor-split` only ever gets one fraction per RPC device — llama.cpp's device
table is capped at 16 entries total and never gives the host's CPU backend a slot —
so a placement across the host + 15 children needs the split limited to the 15 RPC
endpoints, not 16. At 16 boards this is no longer a corner case: it's the normal
shape of a full cluster, so the placement logic drops the host's share from the
`--tensor-split` argument (it's still counted for the RAM fit check, just never a
split target).

This is a proof that pooling scales to this size at all, not a usable chat
experience: at ~7 s/token, even a three-word reply takes minutes, and every token
still has to cross all 15 RPC hops.

### Power draw

The 16-board configuration drew approximately 15 W in total during testing, or about
1 W per board. That draw stayed broadly constant during inference because the
pipeline runs model layers sequentially: one board is doing the active CPU work at a
time, rather than every board executing a layer simultaneously.

### Scaling past 15 RPC boards

15 RPC endpoints is a hard ceiling, not a QCluster convention: ggml's backend
scheduler caps total backends (`GGML_SCHED_MAX_BACKENDS`, `ggml/src/ggml-backend.cpp`)
at 16, and llama-server always adds one CPU backend on top of the RPC devices. Verified
against real hardware — plugging in a 16th child board and letting it into the RPC list
crashes `llama-server` outright with `GGML_ASSERT(n_backends <= GGML_SCHED_MAX_BACKENDS)
failed`, independent of `--tensor-split`. (An earlier version of this note claimed
dropping `--tensor-split` could scale past this; that was wrong — the assert fires at
scheduler creation regardless of the flag.)

So with more than 15 ready child boards, QCluster keeps the 15 with the most usable
RAM and excludes the rest from that load — they stay `ready` and are simply not part of
the current placement. The Models tab shows *(N excluded)* when this happens, and the
board count/weights in the fit tooltip only cover the boards actually in use.



The **Fit** column and the loader share one calculation, on the server:

```
required  = model file size + (ctx / 1024) x 120 MB + 250 MB
available = sum over ready boards of (MemAvailable - reserve)
            + RAM held by the currently loaded model
```

The file size is exact, so the requirement is derived from it rather than from a
per-model guess. Reserves are spike cushions on top of `MemAvailable`, which already
excludes memory in use: 400 MB on the host, 200 MB per child, both overridable with
`QCLUSTER_HOST_RESERVE_MB` and `QCLUSTER_NODE_RESERVE_MB`.

Loading a model always unloads the previous one first, so whatever the current model
occupies counts as available for every other model. The model that is loaded shows
*loaded now*, and any verdict that depends on freeing it is marked *(after unload)*.
Hover the Fit column to see the numbers behind a verdict.

Because availability is measured live, freeing memory on a board — stopping unused
apps, for instance — can move a model from *will not fit* to *needs pooling*.

## Performance expectations

Pooling buys model **size**, not speed. RPC is pipeline-parallel: boards take turns on
their slice of the layers, and activations cross a USB link between them. A 7B model
spread over three boards will answer more slowly than a 0.8B model on one board — it
will just be a much better answer. The UI reports measured tokens/second so the
tradeoff stays visible.

Weights are streamed to each board on first load, which is slow over USB.
`rpc-server --cache` keeps them on the board so subsequent loads of the same model are
fast.

Pooled capacity is what the boards have *free*, not what they have installed. Each
child spends roughly 700–900 MB on Linux and the Arduino app runtime, so two 2 GB
children contribute around 400–550 MB each in practice. Stopping unused apps on the
children is the cheapest way to make room for a bigger model.

## Resilience

- A dead `rpc-server` is detected within one telemetry tick and restarted
  automatically; provisioning is idempotent, so recovery costs one round-trip.
- If a board holding model layers disconnects, `llama-server` cannot continue —
  the daemon stops it and reports which board went away, rather than leaving a wedged
  process behind.
- Unplug and replug a board and it is rediscovered, keeps its original slot (and
  therefore its RPC port), and is reprovisioned without any manual step.


## Security notes

- `rpc-server` and the RPC protocol are unauthenticated by design, so they never leave
  loopback; ADB port forwarding is the only path in.
- Model downloads are restricted to `huggingface.co` and to URLs in
  `daemon/models.json`. The API takes a catalog id or a validated Hugging Face file
  URL, never an arbitrary host, so it cannot be turned into a general fetcher.
- Set `QCLUSTER_TOKEN` to require a bearer token on `/v1/*` and every `/api/*` call.
  The gateway binds `0.0.0.0` so it is reachable from your LAN — set the token if that
  network is not trusted. The web UI will prompt for it and remember it.
- Stored board sudo passwords are verified with `sudo -k`, kept in a `0600` file,
  passed on stdin, and never logged or returned by the API. Storing them is optional.
- Every `adb` invocation uses an argv list, never a shell string, and device serials are
  validated against a strict pattern before use.

## Licence

QCluster is licensed under the [Mozilla Public License 2.0](https://www.mozilla.org/MPL/2.0/)
([SPDX: MPL-2.0](https://spdx.org/licenses/MPL-2.0.html)).

This license applies only to the original QCluster source in this repository.
Third-party software, model files, and tools used or downloaded by QCluster remain
subject to their own licenses and notices.
