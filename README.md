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
mkdir -p ~/.config/systemd/user
cp scripts/qclusterd.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now qclusterd
```

Or in the foreground: `PYTHONPATH=daemon python3 -m qclusterd`

Open `http://<host-board-ip>:7000`.

## Using it

- **Cluster** — every board with live CPU, RAM, temperature and RPC state; pooled RAM
  total; reprovision and RPC restart controls; the live `llama-server` log.
- **Models** — allowlisted catalog with a fit indicator (*fits host alone* / *needs
  pooling* / *will not fit*), resumable downloads, and load/unload.
- **Chat** — streaming chatbox with system prompt and sampler controls, showing
  time-to-first-token and total latency.
- **API** — the base URL, current model name, and copy-paste `curl` / `openai` /
  `requests` snippets.

Any OpenAI client works:

```python
from openai import OpenAI
client = OpenAI(base_url="http://<host-board-ip>:7000/v1", api_key="not-needed")
```

## Measured results

Three boards: one UNO Q 4 GB host plus two UNO Q 2 GB children on a powered USB hub.

| | SmolLM2-360M Q4 | Qwen2.5-1.5B Q4 |
|---|---|---|
| Model file | 259 MB | 986 MB |
| Fits the host board alone? | yes | **no** (820 MB free vs 1740 MB needed) |
| RSS on host `llama-server` | — | 211 MB |
| RSS on child slot 1 `rpc-server` | 189 MB | 485 MB |
| RSS on child slot 2 `rpc-server` | 215 MB | 547 MB |
| Generation | 8.1 tok/s | 2.8 tok/s |
| Prompt processing | 16.6 tok/s | 5.2 tok/s |

The 1.5B row is the point of the project: most of the model lives on the two child
boards, and the host — which could not have held it alone — contributes only 211 MB.
The cost is throughput, exactly as expected from pipeline-parallel RPC.

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
- Model downloads are restricted to URLs in `daemon/models.json`. The API accepts a
  catalog id, never a URL, so it cannot be turned into an arbitrary-fetch endpoint.
- Set `QCLUSTER_TOKEN` to require a bearer token on `/v1/*` and every mutating
  `/api/*` call. The gateway binds `0.0.0.0` so it is reachable from your LAN — set the
  token if that network is not trusted.
- Every `adb` invocation uses an argv list, never a shell string, and device serials are
  validated against a strict pattern before use.

## Licence

MPL-2.0, matching the Arduino app examples this builds on.
