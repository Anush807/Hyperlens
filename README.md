# HVOps — Hybrid Virtualization Ops Platform

Inventory, metrics, anomaly detection, 30-day capacity forecasting and
right-sizing recommendations across VMware vSphere and Nutanix — plus a
read-only assistant backed by a **local** model that answers by calling the
platform's own API.

One `make up` brings up the whole stack. No vSphere licence, no Nutanix
hardware, no API keys.

```
vcsim ──┐                   ┌── Postgres (inventory, users, ML output)
        ├── collector ── API ┤
mock ───┘   (Flask)   (FastAPI)── Mongo (time-series metrics)
                        │  │
                        │  └── llama-server (optional, local GGUF model)
                        └───── React + TypeScript dashboard
```

- **Architecture and the reasoning behind it** → [`docs/architecture.md`](docs/architecture.md)
- **Measured ML results, including where the models lose** → [`docs/ml-evaluation.md`](docs/ml-evaluation.md)
- **The build brief** → [`spec/01-implementation.md`](spec/01-implementation.md)

---

## Quickstart

Requirements: Docker with Compose v2, GNU Make, and about 6 GB of free disk.
Node 20+ only if you want to run the frontend outside Docker.

```bash
git clone <this-repo> hvops && cd hvops
cp .env.example .env          # edit if any host port is already taken
make up                       # build and start everything except the LLM
```

Then add the two simulated sources and pull inventory:

```bash
TOKEN=$(curl -s -X POST localhost:8000/auth/login \
  -d "username=admin@hvops.local&password=admin123" | python3 -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')

curl -s -X POST localhost:8000/sources -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"name":"vcsim-lab","type":"vsphere","endpoint":"vcsim:8989","username":"user","password":"pass"}'

curl -s -X POST localhost:8000/sources -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"name":"nutanix-mock","type":"nutanix","endpoint":"http://nutanix-mock:9440","username":"admin","password":"admin"}'

curl -s -X POST localhost:5001/collect/trigger \
  -H "X-Service-Token: change-me-too" -H 'Content-Type: application/json' -d '{}'
```

You should now have 60 vSphere VMs and 25 Nutanix VMs. Give the ML something to
work with:

```bash
make seed      # 180 days of synthetic metrics (~5M points, about 90 seconds)
make train     # Isolation Forest + LSTM + baseline, then generate findings
make evaluate  # rewrites docs/ml-evaluation.md from the measurements
```

Open http://localhost:5173 and sign in as `admin@hvops.local` / `admin123`.

| Service | URL |
|---|---|
| Dashboard | http://localhost:5173 |
| API docs (Swagger) | http://localhost:8000/docs |
| Health | http://localhost:8000/health |
| Collector health | http://localhost:5001/health |
| Nutanix mock | http://localhost:9440/docs |

### Host port conflicts

Every published port is overridable in `.env`, because container-to-container
traffic never uses them:

```
POSTGRES_PORT=5432   MONGO_PORT=27017   API_PORT=8000
COLLECTOR_PORT=5001  FRONTEND_PORT=5173 VCSIM_PORT=8989
NUTANIX_PORT=9440    LLM_PORT=8080
```

On macOS, AirPlay Receiver listens on 5000, which is why the collector defaults
to 5001.

---

## The assistant (optional)

The assistant runs a model on your own machine through `llama.cpp`'s
OpenAI-compatible server. It is in a separate compose profile, so the rest of
the stack works on machines that cannot host it — the assistant endpoints simply
return `503` with the command to start it.

Download a GGUF model into `./models/`. The 7B Q4_K_M is published as **two
shards**; download both and point `llama.cpp` at the first — it loads the rest
itself.

```bash
# Default: ~4.7 GB total, needs about 6 GB of RAM or VRAM
BASE=https://huggingface.co/Qwen/Qwen2.5-7B-Instruct-GGUF/resolve/main
for part in 00001-of-00002 00002-of-00002; do
  curl -fL --retry 3 -o "models/qwen2.5-7b-instruct-q4_k_m-$part.gguf" \
    "$BASE/qwen2.5-7b-instruct-q4_k_m-$part.gguf"
done
```

```bash
# CPU-only fallback: ~2 GB single file, noticeably faster, somewhat worse at
# tool calling
curl -fL -o models/qwen2.5-3b-instruct-q4_k_m.gguf \
  https://huggingface.co/Qwen/Qwen2.5-3B-Instruct-GGUF/resolve/main/qwen2.5-3b-instruct-q4_k_m.gguf
```

With `huggingface-cli` instead, if you have it:

```bash
pip install -U "huggingface_hub[cli]"
huggingface-cli download Qwen/Qwen2.5-7B-Instruct-GGUF \
  "qwen2.5-7b-instruct-q4_k_m-*.gguf" --local-dir models
```

Point `.env` at the file you downloaded and start the profile:

```bash
# 7B (sharded - name the FIRST shard):
#   LLM_MODEL_FILE=qwen2.5-7b-instruct-q4_k_m-00001-of-00002.gguf
# 3B (single file):
#   LLM_MODEL_FILE=qwen2.5-3b-instruct-q4_k_m.gguf
docker compose --profile llm up -d llm
make assistant-eval          # scores tool selection, appends to ml-evaluation.md
```

The first request after startup is slow while the model is read into memory;
`docker compose logs -f llm` shows when it is ready.

Then ask it things on the **Assistant** page:

- *Which VMs are idle?*
- *How much CPU can I reclaim on cluster DC0_C0?*
- *When will datastore LocalDS_0 fill up?*

Each answer shows a collapsible list of the tools it actually called, so you can
check its working. Two things learned from running it for real, both now in the
code:

- Tool output states the absence of a value in words, not as a bare `null`. Given
  `days_to_threshold: null` the 3B invented "approximately 18 days" for a
  datastore at 5.9% used; told "does NOT reach 90% within the next 30 days" it
  reports exactly that.
- The final answer is replayed from the turn the tool loop already generated
  rather than regenerated. Asking the model to continue a conversation ending in
  its own reply returned nothing at all — correct tool call, blank answer.

**Measured here** on `qwen2.5-3b-instruct` (the spec's CPU fallback, chosen
because this machine's Docker VM is 7.7 GB and the 7B needs ~6 GB to run):
**tool-selection accuracy 100%**, argument accuracy 95% across the 20 cases, in
21 seconds. The one miss is in
[`docs/ml-evaluation.md`](docs/ml-evaluation.md) — it answered "which VMs need
more memory" with `downsize_mem`. Re-run `make assistant-eval` after any model
change and keep the row.

**Swapping models** is an `.env` change. Nothing in the codebase knows which
model is running — the API talks to an OpenAI-compatible endpoint, and the tool
definitions are generated from pydantic schemas. If a newer Qwen instruct
release exists at the same size, it drops in the same way; re-run
`make assistant-eval` and keep the row.

**On a GPU**, switch the `llm` image in `docker-compose.yml` to the CUDA variant
and add `-ngl 99` to its command.

**If the `llm` image tag has disappeared**, ggml-org publishes one tag per
upstream build and prunes old ones. `docker-compose.yml` carries the command for
listing the current tags next to the pinned one.

---

## Make targets

```
make up            Build and start the stack (PROFILE=llm to include the model)
make down          Stop it
make reset         Stop it and delete the data volumes
make logs SVC=api  Tail logs
make seed          Backfill DAYS=180 of synthetic metrics, then roll them up
make rollup        Re-aggregate metrics_daily from raw points
make train         Train both models and regenerate findings
make evaluate      Rewrite docs/ml-evaluation.md from measurements
make assistant-eval  Score the assistant's tool selection (needs the llm profile)
make test          Every suite: API, collector, frontend
make lint          ruff + mypy + eslint + tsc
make fmt           Format Python and TypeScript
make types         Regenerate the frontend's API types from the live OpenAPI doc
```

`make seed DAYS=30 INTERVAL=900` is a much faster option on a laptop: 30 days at
15-minute resolution still exercises every code path.

---

## What it does

### Inventory

Both adapters implement one `HypervisorAdapter` interface and return the same
normalized models, so a vSphere VM and a Nutanix VM are the same kind of row by
the time they reach the database. Upserts are keyed on
`(source_id, external_id)`; a VM that disappears is soft-deleted, never dropped.

One source failing is isolated — stop the Nutanix mock and that source is marked
`failed` while vSphere keeps collecting.

### Metrics

A deterministic synthetic generator assigns every VM a profile (`steady`,
`diurnal`, `growing`, `idle`, `overprovisioned`, `hot`, `anomalous`) and injects
anomaly windows at known absolute timestamps, recorded in
`ml_artifacts/ground_truth.json`. That file is why the ML numbers below are
measurements rather than impressions.

Raw points land in a Mongo time-series collection at 5-minute resolution; an
hourly job rolls them into `metrics_daily`, which is all the ML reads.

### Anomaly detection

An Isolation Forest over hourly windows, where every feature is the entity's
deviation from **its own** behaviour at the same hour on comparable days.
Consecutive flagged hours merge into one incident.

Measured against the injected ground truth: **F1 0.98** at the window level
(precision 1.00, recall 0.95). The ablation in
[`docs/ml-evaluation.md`](docs/ml-evaluation.md) shows that on this synthetic
data the minimum-deviation gate is doing the discriminating and the forest adds
nothing — reported there rather than hidden behind the headline number.

### Capacity forecasting

A PyTorch LSTM (2 layers, hidden 64, direct 30-day head) against a
seasonal-naive baseline, scored on a held-out final 30 days.

**The baseline currently wins overall, so the baseline is what the UI leads
with.** The LSTM wins on four of seven profiles and loses on `growing` and
`diurnal`; both models are stored and drawn on every chart, and which one is the
default is read from the measured holdout rather than assumed. The full table is
in [`docs/ml-evaluation.md`](docs/ml-evaluation.md).

### Right-sizing

Rules, not a model, over the last 14 days of daily p95 — `idle`,
`downsize_cpu`, `downsize_mem`, `upsize_cpu`, `upsize_mem`. Every
recommendation carries the numbers that triggered it, re-running updates rather
than duplicates, and a VM that stops matching has its recommendation closed.

### Assistant

Seven read-only tools over the service layer, a tool-calling loop capped at four
rounds, and the answer streamed over SSE. Measured on the 3B: **100%
tool-selection accuracy**, 95% argument accuracy.

### Dashboard

Dashboard, VM/host/cluster inventory with filters and pagination, VM detail with
metric and forecast charts, anomaly timeline, capacity ranked by days-to-
threshold, grouped recommendations with reclaimable totals, a streaming
assistant, and source administration. Actions a role cannot perform are hidden —
and refused by the API regardless.

---

## Screenshots

Images are not committed to this repository. To produce them, run through the
quickstart (including `make seed && make train` so the charts have data), then
capture the eight pages listed in
[`docs/screenshots/README.md`](docs/screenshots/README.md) and save them there
under the filenames it gives. The links below resolve once the files exist.

| | |
|---|---|
| ![Dashboard](docs/screenshots/dashboard.png) | ![Virtual machines](docs/screenshots/vms.png) |
| *Dashboard — KPIs, cluster allocation, source health* | *Inventory — both sources in one table* |
| ![VM detail](docs/screenshots/vm-detail.png) | ![Capacity](docs/screenshots/capacity.png) |
| *VM detail — 180 days of metrics and the 30-day forecast* | *Capacity — ranked by days-to-threshold* |
| ![Anomalies](docs/screenshots/anomalies.png) | ![Recommendations](docs/screenshots/recommendations.png) |
| *Anomalies — timeline and triage* | *Recommendations — grouped, with reclaimable totals* |
| ![Assistant](docs/screenshots/assistant.png) | ![Sources](docs/screenshots/sources.png) |
| *Assistant — streamed answer with its tool calls shown* | *Sources — admin-only, with Sync now* |

---

## Testing

```bash
make test                                            # everything
docker compose run --rm api pytest -q                # API only
docker compose run --rm api pytest tests/test_ml_models.py -q -k rightsizing
docker compose run --rm --no-deps collector pytest -q
cd frontend && npx vitest run
```

**272 tests**: 210 API (auth and refresh rotation, role guards, ingest
idempotency and soft delete, pagination, metric resolutions and rollups, the ML
models on hand-checked series, the ML jobs end to end against real Postgres and
Mongo, the assistant loop against a scripted model), 49 collector (both
adapters against recorded fixtures, per-source failure isolation, the
generator's determinism), 13 frontend (the API client's refresh behaviour and
role ordering).

Coverage is **80%** on `services/api/app` and `ml`. The API suite needs
Postgres and Mongo and creates a throwaway database per run; the collector and
frontend suites need nothing.

A Postman collection covering **all 30 public endpoints** is in
[`postman/`](postman/), with an environment and a login request that stores the
token for everything after it.

---

## Repository layout

```
docker-compose.yml        Pinned images; the llm service is in its own profile
Makefile                  Every workflow in this README
docs/                     architecture.md, ml-evaluation.md (generated)
postman/                  Collection + environment
spec/                     The implementation brief
services/
  api/                    FastAPI: routes/, services/, db/, assistant/, ml/, alembic/
  collector/              Flask: adapters/, metrics/, routes/, scheduler
  nutanix-mock/           Prism Central v3 mock + fixtures
  synthetic/              scenarios.yaml + generate_history.py
frontend/                 Vite + React + TypeScript
models/                   GGUF files (gitignored)
ml_artifacts/             Trained models + ground_truth.json (gitignored)
```

---

## Known limitations

- **vcsim's VMs are 1 vCPU / 32 MB.** That is the simulator's default and the
  adapter reports it faithfully, so downsize recommendations can only fire on
  the (realistically sized) Nutanix fixtures. The synthetic profile assignment
  accounts for this: `overprovisioned` is only assigned to a VM that has
  something to give back.
- **The LSTM does not beat the baseline.** Deliberately shipped and reported
  that way. On near-deterministic weekly-periodic synthetic data, seasonal naive
  is close to the generating process; a real estate's noise is where the LSTM
  would earn its keep.
- **The anomaly F1 flatters the Isolation Forest.** See the ablation.
- **Metrics are synthetic.** `MetricsProvider` is the seam a real vSphere or
  Prism provider plugs into; no other code changes.
- **vcsim is not vCenter.** Every pyVmomi property access is wrapped in a
  defensive getter because the simulator leaves optional properties unset.
- **The `growing` profile's trend is a 180-day sawtooth.** At ~0.28%/day a trend
  cannot run much longer before saturating, so it restarts. Every window the
  platform evaluates is shorter than one ramp.
- **CPU inference is slow.** Expect tens of seconds for a 7B answer on CPU.
  Responses stream, and the 3B fallback is much faster.
- **Single tenant, read-only.** No write actions against any hypervisor.

---

## Roadmap (V2)

- Real vSphere performance counters and Prism stats behind `MetricsProvider`
- Django admin and reporting portal
- CMDB import from MySQL / SQL Server
- A TensorFlow comparison model alongside the PyTorch one
- Write actions — power operations, snapshots, migrations — with approvals
- Multi-tenancy
- E2E browser tests

---

## Configuration

Everything is environment-driven; see [`.env.example`](.env.example) for the
full list with comments. The ones worth knowing:

| Variable | Why you would change it |
|---|---|
| `SEED_ADMIN_EMAIL` / `SEED_ADMIN_PASSWORD` | The account seeded on an empty database |
| `JWT_SECRET`, `INTERNAL_SERVICE_TOKEN` | **Change both before exposing this anywhere** |
| `FERNET_KEY` | Encrypts stored hypervisor passwords. Rotating it makes existing credentials unreadable |
| `POLL_INVENTORY_SECONDS` / `POLL_METRICS_SECONDS` | Collector cadence |
| `LLM_MODEL_FILE`, `LLM_CTX` | Which GGUF to serve, and its context window |
| `CPU_THRESHOLD_PCT`, `DATASTORE_THRESHOLD_PCT` | What "running out" means for days-to-threshold |

The defaults are development defaults. `JWT_SECRET=change-me` and
`INTERNAL_SERVICE_TOKEN=change-me-too` are exactly as safe as they look.
