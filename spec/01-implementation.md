# 01 - Implementation Spec: HVOps V1

Hybrid Virtualization Ops Platform with AI-driven capacity planning.
This document is the implementation brief for V1. Build it phase by phase, in order. Each phase ends with acceptance criteria that must pass before the next phase starts.

---

## 1. V1 Goal

A single `docker compose up` brings up a working platform that:

1. Discovers inventory (clusters, hosts, VMs, datastores) from a simulated vCenter and a mocked Nutanix Prism.
2. Stores normalized inventory in PostgreSQL and per-VM / per-host metrics in MongoDB.
3. Detects anomalies (Isolation Forest), forecasts capacity 30 days ahead (PyTorch LSTM vs. a naive baseline), and generates right-sizing recommendations.
4. Exposes everything through an authenticated, documented FastAPI.
5. Shows it all in a React + TypeScript dashboard.
6. Includes a read-only AI assistant, backed by a local Hugging Face model, that answers questions by calling the platform's own API.

### In scope (V1)

- Flask collector service (vSphere adapter + Nutanix adapter)
- FastAPI core API (auth, inventory, metrics, anomalies, forecasts, recommendations, assistant)
- PostgreSQL (relational data), MongoDB (time-series metrics and events)
- Synthetic metric generator with known injected anomalies and trends
- ML: Isolation Forest, PyTorch LSTM forecaster, rule-based right-sizing
- Local LLM served through an OpenAI-compatible endpoint, with tool calling
- React + TypeScript dashboard
- pytest suite, Postman collection, Docker Compose

### Out of scope (V2+)

- Django admin/reporting portal
- MySQL / SQL Server CMDB import
- TensorFlow comparison model
- Write actions against hypervisors (power ops, snapshots, migrations)
- Multi-tenancy
- Real vCenter / Nutanix performance counters (V1 uses synthetic metrics behind an interface, so real providers drop in later)

---

## 2. Key Decisions

| Area | Decision | Reason |
|---|---|---|
| vCenter | `vcsim` (govmomi simulator) in Docker, accessed with `pyVmomi` | Real vSphere API surface without a lab |
| Nutanix | Small FastAPI mock implementing Prism v3 list endpoints from fixture JSON | Nutanix CE needs heavy hardware; the adapter code stays real |
| Metrics | `MetricsProvider` interface; V1 uses `SyntheticMetricsProvider` | vcsim perf counters are not realistic; synthetic data gives ground truth for evaluating ML |
| Service boundaries | Collector never touches databases; it POSTs normalized batches to internal ingest endpoints on the API | One service owns each datastore |
| Python ORM | SQLAlchemy 2.x (async) + Alembic | Prisma equivalent for Python; typed models and migrations |
| Mongo driver | `motor` (async) | Matches FastAPI async |
| Mongo storage | Native time-series collections | Built for this workload |
| LLM serving | `llama.cpp` server (`llama-server`) with a GGUF model, OpenAI-compatible API | Runs on CPU or GPU, supports tool calling, backend stays model-agnostic |
| LLM model | Default `Qwen2.5-7B-Instruct` GGUF Q4_K_M; CPU-only fallback `Qwen2.5-3B-Instruct` | Reliable tool calling at small sizes. Check Hugging Face for a newer Qwen instruct release with the same size and swap it in through env vars |
| Frontend state | TanStack Query for server state, Zustand for small client state | Recoil was archived by Meta in 2025; avoid building on it |
| UI | Vite, React 18, TypeScript, shadcn/ui, Tailwind, Recharts, React Router | |
| Routing (both backends) | One router/blueprint file per resource, in a `routes/` folder | |

---

## 3. Architecture

```
                         +----------------------+
                         |  React + TS (Vite)   |
                         +----------+-----------+
                                    | REST (JWT)
                         +----------v-----------+        +------------------+
                         |   FastAPI core API   +------->+  llama-server    |
                         |  auth, inventory,    | OpenAI |  (GGUF model)    |
                         |  metrics, ML, chat   | compat +------------------+
                         +--+-------+--------+--+
                            |       |        ^
                 SQLAlchemy |       | motor  | internal ingest (service token)
                            v       v        |
                    +-------+--+ +--+-----+  |
                    | Postgres | | Mongo  |  |
                    +----------+ +--------+  |
                                             |
                                 +-----------+-----------+
                                 |  Flask collector      |
                                 |  APScheduler polling  |
                                 +-----+-----------+-----+
                                       |           |
                                pyVmomi|           |HTTP
                                 +-----v--+   +----v-------------+
                                 | vcsim  |   | nutanix-mock     |
                                 +--------+   | (Prism v3 shape) |
                                              +------------------+
```

ML training runs as a CLI job (`python -m ml.train ...`) inside the API image. It writes model artifacts to a shared volume. The API loads those artifacts at startup and on `POST /ml/reload`.

---

## 4. Repository Layout

```
hvops/
  docker-compose.yml
  .env.example
  Makefile
  README.md
  docs/
    01-implementation.md
    architecture.md
    ml-evaluation.md
  postman/
    hvops.postman_collection.json
  services/
    collector/                      # Flask
      app/
        __init__.py                 # create_app()
        config.py
        routes/
          health.py
          collect.py
        adapters/
          base.py                   # HypervisorAdapter ABC
          vsphere.py                # pyVmomi
          nutanix.py                # Prism v3 REST
          factory.py                # get_adapter(source)
        metrics/
          base.py                   # MetricsProvider ABC
          synthetic.py              # SyntheticMetricsProvider
        schemas.py                  # normalized pydantic models
        scheduler.py                # APScheduler jobs
        ingest_client.py            # POSTs to API /internal/*
      tests/
      Dockerfile
      pyproject.toml
    api/                            # FastAPI
      app/
        main.py
        config.py
        deps.py                     # db sessions, current_user, role guards
        db/
          postgres.py
          mongo.py
          models/                   # SQLAlchemy models, one file per table group
        schemas/                    # pydantic request/response models
        routes/
          auth.py
          sources.py
          inventory.py
          metrics.py
          anomalies.py
          forecasts.py
          recommendations.py
          assistant.py
          internal.py               # ingest endpoints, service-token only
          ml_admin.py
        services/                   # business logic, no FastAPI imports
        assistant/
          llm_client.py
          tools.py
          prompts.py
      ml/
        data.py                     # load + preprocess from Mongo with pandas
        anomaly.py                  # Isolation Forest
        forecast.py                 # LSTM + naive baseline
        rightsizing.py
        train.py                    # CLI entry
        evaluate.py
      alembic/
      tests/
      Dockerfile
      pyproject.toml
    nutanix-mock/
      app.py
      fixtures/
        clusters.json
        hosts.json
        vms.json
      Dockerfile
    synthetic/
      generate_history.py           # backfill N days of metrics
      scenarios.yaml                # which VMs get which patterns/anomalies
  frontend/
    src/
      api/                          # typed fetch client + query hooks
      components/
      pages/
      stores/                       # zustand
      routes.tsx
    Dockerfile
  models/                           # gitignored; GGUF files live here
  ml_artifacts/                     # gitignored; trained models live here
```

---

## 5. Environment

`.env.example`:

```
# Postgres
POSTGRES_USER=hvops
POSTGRES_PASSWORD=hvops
POSTGRES_DB=hvops
DATABASE_URL=postgresql+asyncpg://hvops:hvops@postgres:5432/hvops

# Mongo
MONGO_URL=mongodb://mongo:27017
MONGO_DB=hvops

# Auth
JWT_SECRET=change-me
JWT_ACCESS_TTL_MIN=15
JWT_REFRESH_TTL_DAYS=7
SEED_ADMIN_EMAIL=admin@hvops.local
SEED_ADMIN_PASSWORD=admin123

# Internal service auth
INTERNAL_SERVICE_TOKEN=change-me-too

# Collector
API_INTERNAL_URL=http://api:8000/internal
POLL_INVENTORY_SECONDS=300
POLL_METRICS_SECONDS=60
VSPHERE_HOST=vcsim
VSPHERE_PORT=8989
VSPHERE_USER=user
VSPHERE_PASSWORD=pass
VSPHERE_INSECURE=true
NUTANIX_URL=http://nutanix-mock:9440
NUTANIX_USER=admin
NUTANIX_PASSWORD=admin

# LLM
LLM_BASE_URL=http://llm:8080/v1
LLM_MODEL=qwen2.5-7b-instruct
LLM_MODEL_FILE=qwen2.5-7b-instruct-q4_k_m.gguf
LLM_CTX=8192
LLM_TEMPERATURE=0.2

# ML
ML_ARTIFACT_DIR=/ml_artifacts
```

Pin every image version and package version. Do not use `latest` anywhere.

---

## 6. Data Model

### 6.1 Normalized schema (shared by both adapters)

Defined in `collector/app/schemas.py` and mirrored in `api/app/schemas/ingest.py`.

```python
class SourceType(str, Enum):
    VSPHERE = "vsphere"
    NUTANIX = "nutanix"

class ClusterIn(BaseModel):
    external_id: str
    name: str

class HostIn(BaseModel):
    external_id: str
    cluster_external_id: str | None
    name: str
    cpu_cores: int
    cpu_mhz_per_core: int | None
    memory_mb: int
    power_state: Literal["on", "off", "unknown"]

class VMIn(BaseModel):
    external_id: str
    host_external_id: str | None
    name: str
    vcpus: int
    memory_mb: int
    provisioned_storage_gb: float
    guest_os: str | None
    power_state: Literal["on", "off", "suspended", "unknown"]

class DatastoreIn(BaseModel):
    external_id: str
    name: str
    capacity_gb: float
    free_gb: float

class InventoryBatch(BaseModel):
    source_id: UUID
    collected_at: datetime
    clusters: list[ClusterIn]
    hosts: list[HostIn]
    vms: list[VMIn]
    datastores: list[DatastoreIn]

class MetricPoint(BaseModel):
    entity_type: Literal["vm", "host"]
    entity_external_id: str
    ts: datetime
    cpu_pct: float
    mem_pct: float
    disk_iops: float
    disk_latency_ms: float
    net_kbps: float

class MetricsBatch(BaseModel):
    source_id: UUID
    points: list[MetricPoint]
```

### 6.2 PostgreSQL tables

All tables have `id UUID PK`, `created_at`, and `updated_at`. Inventory tables have `UNIQUE(source_id, external_id)` and are upserted on ingest.

| Table | Key columns |
|---|---|
| `users` | email (unique), password_hash (argon2), role (`admin` / `operator` / `viewer`), is_active |
| `refresh_tokens` | user_id, token_hash, expires_at, revoked |
| `hypervisor_sources` | name, type (`vsphere` / `nutanix`), endpoint, is_enabled, last_inventory_at, last_status, last_error |
| `clusters` | source_id, external_id, name |
| `hosts` | source_id, cluster_id, external_id, name, cpu_cores, cpu_mhz_per_core, memory_mb, power_state, last_seen_at |
| `vms` | source_id, host_id, external_id, name, vcpus, memory_mb, provisioned_storage_gb, guest_os, power_state, last_seen_at, is_deleted |
| `datastores` | source_id, external_id, name, capacity_gb, free_gb |
| `anomalies` | entity_type, entity_id, detected_at, window_start, window_end, score, features (JSONB), status (`open` / `acknowledged` / `resolved`), summary (text, LLM-generated, nullable) |
| `forecasts` | entity_type, entity_id, metric, model (`lstm` / `naive`), generated_at, horizon_days, points (JSONB `[{date, value, lower, upper}]`), days_to_threshold (nullable int) |
| `recommendations` | vm_id, kind (`downsize_cpu` / `downsize_mem` / `upsize_cpu` / `upsize_mem` / `idle`), current_value, suggested_value, evidence (JSONB), generated_at, status (`open` / `dismissed`) |
| `assistant_conversations` | user_id, title |
| `assistant_messages` | conversation_id, role (`user` / `assistant` / `tool`), content, tool_name, tool_args (JSONB), created_at |
| `audit_log` | user_id, action, entity_type, entity_id, details (JSONB), created_at |

VMs missing from a full inventory batch for their source get `is_deleted = true`. Never hard delete.

### 6.3 MongoDB collections

- `metrics` is a time-series collection with `timeField: "ts"`, `metaField: "meta"` (`{source_id, entity_type, entity_external_id}`), and `granularity: "minutes"`. Use a TTL of 180 days.
- `metrics_daily` is a regular collection of pre-aggregated daily rows (`mean`, `p95`, `max` per metric). A rollup job in the API writes it every hour. The ML code reads from here.
- `events` stores collector run logs and ingest errors, with a TTL of 30 days.

Indexes: `metrics_daily` on `(entity_type, entity_external_id, date)` unique.

---

## 7. Collector Service (Flask)

### 7.1 Patterns

- **Adapter**: `HypervisorAdapter` ABC with `connect()`, `list_clusters()`, `list_hosts()`, `list_vms()`, `list_datastores()`, `close()`. All of them return normalized models.
- **Factory**: `get_adapter(source: SourceConfig) -> HypervisorAdapter`, keyed by `SourceType`.
- **Strategy**: `MetricsProvider` ABC with `collect(entities, since) -> list[MetricPoint]`. V1 ships `SyntheticMetricsProvider` only.

### 7.2 vSphere adapter

- Connect with `SmartConnect`. If `VSPHERE_INSECURE` is set, use an unverified SSL context; log a warning once.
- Walk inventory with `ContainerView` per type (`vim.ClusterComputeResource`, `vim.HostSystem`, `vim.VirtualMachine`, `vim.Datastore`).
- Map from `summary.config`, `summary.hardware`, and `runtime.powerState`. Use `moId` (`_moId`) as `external_id`.
- Retry connection with exponential backoff (3 attempts) and disconnect in `finally`.

### 7.3 Nutanix adapter

- `POST {NUTANIX_URL}/api/nutanix/v3/{clusters|hosts|vms}/list` with basic auth and body `{"kind": "...", "length": 500, "offset": N}`. Paginate until `metadata.total_matches` is reached.
- Map Prism v3 fields: `metadata.uuid` to `external_id`, `spec.resources.num_sockets * num_vcpus_per_socket` to `vcpus`, `spec.resources.memory_size_mib` to `memory_mb`, `spec.resources.power_state` to `power_state` (normalized).
- Nutanix has no datastore concept in the same shape. Return storage containers as datastores if the mock provides them; otherwise return an empty list.

### 7.4 Nutanix mock

A FastAPI app of about 60 lines serving the three list endpoints from `fixtures/*.json` in real Prism v3 response shape (`api_version`, `metadata`, `entities`). It supports `offset` and `length` and checks basic auth. Fixture contents: 1 cluster, 3 hosts, 25 VMs with varied sizes and power states.

### 7.5 vcsim

Run with flags to produce a reasonable topology, for example `-cluster 2 -host 3 -vm 10`. That gives about 60 VMs across 2 clusters. Pin the image tag.

### 7.6 Synthetic metrics

`SyntheticMetricsProvider` is deterministic per entity: it seeds an RNG from `hash(entity_external_id)`. Each VM is assigned a profile from `synthetic/scenarios.yaml`:

| Profile | Behavior |
|---|---|
| `steady` | Flat baseline with noise |
| `diurnal` | Daily sine wave (business hours peak) and a weekly dip on weekends |
| `growing` | Diurnal plus a linear upward trend (used to test forecasting) |
| `idle` | CPU under 5% (used to test right-sizing) |
| `overprovisioned` | p95 CPU under 20% on many vCPUs |
| `hot` | p95 CPU above 85% |
| `anomalous` | Diurnal plus injected anomaly windows (spikes, latency storms, memory leaks) at timestamps listed in the YAML |

`synthetic/generate_history.py --days 180` backfills history at 5-minute resolution by POSTing batches to the ingest endpoint in chunks of 5,000 points. Injected anomaly windows are also written to `synthetic/ground_truth.json` so anomaly detection can be scored.

### 7.7 Scheduling and routes

- APScheduler `BackgroundScheduler` with two jobs: inventory every `POLL_INVENTORY_SECONDS` and metrics every `POLL_METRICS_SECONDS`.
- Sources are fetched from `GET /internal/sources` on the API at each run, so adding a source in the UI is picked up without a restart.
- Use one failure domain per source: one source failing must not stop the others. Report the status with `POST /internal/sources/{id}/status`.

Routes:

- `GET /health` returns liveness and the last run time per job.
- `POST /collect/trigger` (service token) runs inventory collection immediately. The API's "Sync now" button calls this.

---

## 8. Core API (FastAPI)

### 8.1 Cross-cutting

- Async everywhere: `asyncpg` with SQLAlchemy async sessions, and `motor`.
- Auth: OAuth2 password flow issuing a JWT access token (15 min) and a refresh token (7 days, stored hashed, rotated on use).
- Role guards as dependencies: `require_role("viewer")`, `require_role("operator")`, `require_role("admin")`, ordered so that admin > operator > viewer.
- `/internal/*` is protected by an `X-Service-Token` header only and is excluded from the public OpenAPI schema.
- Pagination: `?limit=&offset=` with a response envelope `{items, total, limit, offset}`.
- Errors: a consistent `{error: {code, message, details}}` body via exception handlers.
- CORS restricted to the frontend origin.
- Structured JSON logging with a request ID middleware.
- Add a simple in-process TTL cache (60 s) for the dashboard summary endpoint.
- Seed the admin user on startup if no users exist.

### 8.2 Endpoints

| Method | Path | Role | Purpose |
|---|---|---|---|
| POST | `/auth/login` | public | Returns access + refresh tokens |
| POST | `/auth/refresh` | public | Rotates the refresh token |
| POST | `/auth/logout` | viewer | Revokes the refresh token |
| GET | `/auth/me` | viewer | Current user |
| POST | `/users` | admin | Create a user |
| GET | `/sources` | viewer | List hypervisor sources with status |
| POST | `/sources` | admin | Add a source (credentials stored encrypted with Fernet) |
| PATCH | `/sources/{id}` | admin | Enable/disable or edit |
| POST | `/sources/{id}/sync` | operator | Calls collector `/collect/trigger` |
| GET | `/inventory/summary` | viewer | Counts, totals, allocation ratios per cluster |
| GET | `/clusters`, `/hosts`, `/vms`, `/datastores` | viewer | Filterable lists (`source_id`, `cluster_id`, `power_state`, `q`) |
| GET | `/vms/{id}` | viewer | Detail including latest metrics, open recommendations, open anomalies |
| GET | `/metrics/{entity_type}/{id}` | viewer | `?from=&to=&resolution=raw\|hourly\|daily` |
| GET | `/anomalies` | viewer | Filter by status, entity, date |
| PATCH | `/anomalies/{id}` | operator | Acknowledge or resolve |
| GET | `/forecasts/{entity_type}/{id}` | viewer | Latest LSTM and naive forecasts per metric |
| GET | `/capacity/clusters/{id}` | viewer | Cluster-level forecast and days-to-threshold |
| GET | `/recommendations` | viewer | Filter by kind and status; includes estimated reclaimable vCPU and RAM |
| PATCH | `/recommendations/{id}` | operator | Dismiss |
| POST | `/assistant/conversations` | viewer | Start a conversation |
| GET | `/assistant/conversations/{id}` | viewer | Messages |
| POST | `/assistant/conversations/{id}/messages` | viewer | Send a message; returns an SSE stream |
| POST | `/ml/run` | admin | Kicks off anomaly detection, forecasting and right-sizing as a background task |
| POST | `/ml/reload` | admin | Reload artifacts from disk |
| GET | `/ml/status` | admin | Last run times, model versions, evaluation metrics |
| GET | `/internal/sources` | service | Enabled sources with decrypted credentials |
| POST | `/internal/inventory` | service | Upsert an `InventoryBatch` in one transaction |
| POST | `/internal/metrics` | service | Bulk insert into the Mongo `metrics` collection |
| POST | `/internal/sources/{id}/status` | service | Collector run result |

Every mutating endpoint writes to `audit_log`.

### 8.3 Background jobs (inside the API)

Use APScheduler `AsyncIOScheduler`:

- Hourly: roll up `metrics` into `metrics_daily`.
- Hourly: run anomaly detection over the last 24 hours.
- Daily at 02:00: regenerate forecasts and recommendations.

---

## 9. ML Module

All code in `services/api/ml/`. Training and evaluation are CLIs; inference runs in the API.

### 9.1 Data preparation (`data.py`)

- Load from `metrics_daily` into pandas DataFrames indexed by date, one row per entity per day.
- Forward-fill gaps of up to 2 days and drop entities with more than 10% missing days.
- For anomaly detection, build hourly windows from the raw `metrics` collection.

### 9.2 Anomaly detection (`anomaly.py`)

- Features per entity per 1-hour window: mean, p95 and std of `cpu_pct`, `mem_pct`, `disk_latency_ms`, plus mean `disk_iops` and `net_kbps`, plus deltas against the same hour of the previous 7 days (to avoid flagging normal daily peaks).
- Model: scikit-learn `IsolationForest(n_estimators=200, contamination="auto", random_state=42)` in a `Pipeline` with `StandardScaler`. Train one global model on 30 days of windows.
- Threshold: pick the score cutoff from the training-score distribution (for example the 99th percentile). Store it alongside the model.
- Merge consecutive flagged windows for the same entity into a single anomaly row.
- Evaluate against `synthetic/ground_truth.json`: precision, recall, and F1 at the window level. Write them to `docs/ml-evaluation.md`.

### 9.3 Forecasting (`forecast.py`)

- Target metrics: daily p95 `cpu_pct` and `mem_pct` per VM; datastore `used_pct` per datastore; aggregated allocation per cluster.
- **Baseline**: seasonal naive (value from the same weekday last week, repeated forward). Every forecast must beat this or it is not worth shipping.
- **LSTM** (PyTorch):
  - One global model across all entities, with series scaled per entity (min-max fit on the training window).
  - Input: 28 days. Output: 30 days directly (multi-output head), not recursive, to avoid error compounding.
  - Inputs per step: the metric value, day-of-week sin/cos.
  - Architecture: 2-layer LSTM, hidden size 64, dropout 0.2, linear head of size 30.
  - Training: Adam (lr 1e-3), MSE loss, batch 64, early stopping on validation loss (patience 5). Time-based split: last 30 days held out for test, the 30 before that for validation.
  - Uncertainty bands: residual quantiles (10th/90th) from validation, applied per horizon step. Keep it simple; do not attempt Bayesian methods in V1.
- Metrics: MAE and sMAPE on the test window for LSTM and baseline, reported per profile (`growing`, `diurnal`, and so on).
- `days_to_threshold`: first forecast day where the value crosses 85% (CPU/mem) or 90% (datastore), else null.
- Artifacts: `ml_artifacts/forecast/lstm_v{n}.pt`, `scalers_v{n}.json`, `metrics_v{n}.json`.
- Runs on CPU. With around 100 entities and 180 days of history, training should take minutes.

### 9.4 Right-sizing (`rightsizing.py`)

Rules on the last 14 days of daily data, for powered-on VMs only:

| Condition | Recommendation |
|---|---|
| p95 CPU < 5% and p95 net < 10 kbps | `idle` |
| p95 CPU < 20% and vCPUs > 2 | `downsize_cpu` to `max(2, ceil(vcpus * p95 / 50))` |
| p95 mem < 30% and memory > 4 GB | `downsize_mem` to `max(4096, round_up_to_gb(memory_mb * p95 / 60))` |
| p95 CPU > 85% on 5 or more of 14 days | `upsize_cpu` by 50% (rounded up) |
| p95 mem > 90% on 5 or more of 14 days | `upsize_mem` by 50% |

Store the numbers that triggered the rule in `evidence`. Re-running must not duplicate open recommendations: update the existing one instead.

### 9.5 CLI

```
python -m ml.train anomaly --days 30
python -m ml.train forecast --days 180
python -m ml.evaluate --out docs/ml-evaluation.md
python -m ml.run_all            # what POST /ml/run calls
```

---

## 10. AI Assistant (Local Hugging Face Model)

### 10.1 Serving

- Download the GGUF file from Hugging Face into `./models/` (for example with `huggingface-cli download Qwen/Qwen2.5-7B-Instruct-GGUF <file> --local-dir models`). Document the exact command in the README.
- Run `llama-server` in Docker with `--jinja` (required for the model's tool-calling chat template), `-c 8192`, and `--host 0.0.0.0 --port 8080`. Use the CUDA image if a GPU is available and the CPU image otherwise. Pin the image tag.
- The API talks to it with the official `openai` Python client pointed at `LLM_BASE_URL`. Nothing else in the code knows which model is running.
- Hardware guide: the 7B Q4_K_M needs about 6 GB of RAM or VRAM. On CPU it is slow but usable for short answers. If responses are too slow, switch to the 3B model by changing `LLM_MODEL_FILE`; tool-calling accuracy drops somewhat.

### 10.2 Tools

The tools are thin wrappers around the service layer (not HTTP calls back into the API). Every tool is read-only and runs with the calling user's role.

| Tool | Args | Returns |
|---|---|---|
| `get_inventory_summary` | `cluster_name?` | Counts and allocation per cluster |
| `search_vms` | `name_contains?`, `cluster_name?`, `power_state?`, `min_vcpus?`, `limit` (max 20) | Compact VM list |
| `get_vm_details` | `vm_name` | Specs, latest metrics, open recommendations and anomalies |
| `get_metric_stats` | `entity_type`, `name`, `metric`, `days` (max 90) | mean, p95, max, trend slope |
| `list_anomalies` | `status?`, `days?`, `limit` | Compact list |
| `list_recommendations` | `kind?`, `cluster_name?`, `limit` | Compact list plus total reclaimable vCPU/RAM |
| `get_capacity_forecast` | `cluster_name` or `datastore_name` | days_to_threshold per metric plus a short series |

Tool outputs must be compact JSON (truncate lists, round floats to 1 decimal place) because context is limited to 8k tokens.

### 10.3 Loop

1. Build messages: system prompt, the last 10 conversation messages, and the new user message.
2. Call the model with `tools` and `tool_choice="auto"`.
3. If it returns tool calls: validate the arguments with pydantic, execute them, append the results as `tool` messages, and call the model again.
4. Cap the loop at 4 tool rounds. If the cap is reached, return what is available and say the question was too broad.
5. Stream the final answer to the client over SSE. Persist all messages, including tool calls, in Postgres.
6. If argument validation fails, send the validation error back to the model as the tool result once. If it fails again, return an error message to the user.

### 10.4 System prompt rules

- You are an operations assistant for a virtualization platform. Answer only from tool results. If the tools do not return the data, say so; never invent VM names or numbers.
- You cannot change anything. If the user asks for an action, explain that V1 is read-only and point them to the relevant screen.
- Keep answers short; use a table when listing more than 3 items.
- Mention the time window of any statistic you report.

### 10.5 Anomaly summaries

When a new anomaly is created, a background task asks the model for a two-sentence summary from the anomaly features and the entity's recent stats (no tools; one call). The result is stored in `anomalies.summary`. If the LLM is down, leave it null; never block anomaly creation.

### 10.6 Evaluation

Create `api/tests/assistant_eval.yaml` with 20 questions and their expected tool calls (for example, "Which VMs are idle?" should call `list_recommendations(kind="idle")`). Write a script that runs them against the live model and reports the tool-selection accuracy and argument accuracy. Record the result for each model you try in `docs/ml-evaluation.md`. This is how you justify the model choice.

---

## 11. Frontend

Vite + React 18 + TypeScript (strict), shadcn/ui, Tailwind, TanStack Query, Zustand, React Router, Recharts.

### 11.1 Pages

| Route | Content |
|---|---|
| `/login` | Login form |
| `/` | Dashboard: KPI cards (VMs, hosts, powered-on ratio, open anomalies, reclaimable vCPU/RAM), cluster allocation bars, source health, recent anomalies |
| `/inventory/vms` | Data table with filters, search, pagination, and a source badge (vSphere / Nutanix) |
| `/inventory/vms/:id` | Specs, metric charts (CPU/mem/disk/net, range selector), forecast chart with bands and threshold line, recommendations, anomalies |
| `/inventory/hosts`, `/inventory/clusters` | Tables with drill-down links |
| `/anomalies` | Timeline plus table; acknowledge/resolve for operators |
| `/capacity` | Per-cluster and per-datastore forecasts sorted by days-to-threshold |
| `/recommendations` | Grouped by kind with a reclaimable-resources summary; dismiss for operators |
| `/assistant` | Chat with streamed responses; collapsible "tool calls used" section under each answer |
| `/settings/sources` | Admin: add, edit, enable/disable sources; "Sync now" |

### 11.2 Rules

- Generate TypeScript types from the FastAPI OpenAPI schema (`openapi-typescript`). Do not hand-write API types.
- Access token in memory (Zustand), refresh token in an httpOnly cookie set by the API. One retry on 401 after a refresh.
- Hide actions the user's role cannot perform; the API still enforces it.
- Every page handles loading, empty and error states.

---

## 12. Docker Compose

Services: `postgres`, `mongo`, `vcsim`, `nutanix-mock`, `llm`, `api`, `collector`, `frontend`.

- Health checks on `postgres`, `mongo`, `api`, and `llm`. `api` depends on healthy databases; `collector` depends on a healthy `api`.
- `api` runs `alembic upgrade head` on start before Uvicorn.
- Volumes: `pgdata`, `mongodata`, `./models:/models:ro` for `llm`, `./ml_artifacts:/ml_artifacts` for `api`.
- `llm` is in a compose profile (`--profile llm`), so the rest of the stack runs on machines that cannot host the model. The assistant endpoints return HTTP 503 with a clear message when the LLM is unreachable.

`Makefile` targets: `up`, `down`, `logs`, `seed` (generate 180 days of synthetic history), `train`, `test`, `lint`, `fmt`, `types` (regenerate frontend API types).

---

## 13. Testing

- **Collector**: unit tests for both adapters with recorded fixture responses (pyVmomi objects mocked; Nutanix via `respx`). Test the normalized output, pagination, and that one failing source does not stop the others.
- **API**: pytest + httpx `AsyncClient`, against Postgres and Mongo started with testcontainers. Cover auth flows (login, refresh rotation, revoked token), role guards on every mutating endpoint, inventory upsert idempotency, the soft-delete of missing VMs, and pagination.
- **ML**: deterministic tests on small synthetic series. The `growing` profile must produce a positive trend and a non-null `days_to_threshold`; injected spikes must be flagged; right-sizing rules must produce the expected recommendation for each profile; reruns must not duplicate recommendations.
- **Assistant**: unit tests with a fake LLM client returning scripted tool calls; test the round cap, invalid-argument handling and persistence.
- **Frontend**: Vitest for the API client and auth refresh logic. No E2E in V1.
- Linting: `ruff` and `mypy` for Python; ESLint and `tsc --noEmit` for the frontend.
- **Postman**: a collection with an environment, a login request that stores the token, and one request per public endpoint.

---

## 14. Build Phases

### Phase 1: Skeleton and infrastructure

- Repo layout, pyproject files, Dockerfiles, compose with Postgres, Mongo, vcsim, the Nutanix mock, an empty FastAPI app and an empty Flask app.
- Alembic set up with all tables from 6.2; Mongo collections and indexes created on API startup.

**Accept when:** `make up` starts everything healthy; `/docs` loads; `alembic upgrade head` creates every table; `mongosh` shows the time-series collection.

### Phase 2: Auth and sources

- Users, JWT, refresh rotation, role guards, admin seed, sources CRUD with encrypted credentials, audit log.

**Accept when:** auth and role tests pass; a viewer gets 403 on `POST /sources`.

### Phase 3: Collector and inventory

- Both adapters, factory, scheduler, internal ingest endpoints, upsert logic, soft-delete, source status reporting.

**Accept when:** about 60 vSphere VMs and 25 Nutanix VMs appear in `/vms` within one poll cycle; stopping the Nutanix mock marks that source as failed without affecting vSphere; re-ingesting the same batch changes nothing.

### Phase 4: Metrics

- Synthetic provider, `generate_history.py`, metrics ingest, hourly and daily rollups, `/metrics` endpoint with resolutions.

**Accept when:** `make seed` loads 180 days for every VM and host; `/metrics/vm/{id}?resolution=daily` returns 180 rows; a `growing` VM visibly trends upward.

### Phase 5: ML

- Anomaly detection, LSTM and baseline forecasting, right-sizing, CLI, scheduled jobs, `/ml/*`, `/anomalies`, `/forecasts`, `/capacity`, `/recommendations`.

**Accept when:** anomaly F1 against ground truth is 0.7 or better; LSTM MAE beats the seasonal naive baseline on the `growing` and `diurnal` profiles (if it does not, report that honestly in `ml-evaluation.md` and ship the baseline as the default); every `idle`, `overprovisioned` and `hot` VM gets the matching recommendation.

### Phase 6: Assistant

- `llama-server` in compose, LLM client, tools, loop, SSE streaming, persistence, anomaly summaries, eval script.

**Accept when:** "Which VMs are idle?", "How much CPU can I reclaim on cluster X?" and "When will datastore Y fill up?" all return answers grounded in tool results; the eval script reports tool-selection accuracy of 80% or better; the API returns 503 cleanly when the `llm` service is stopped.

### Phase 7: Frontend

- All pages from 11.1, generated types, auth flow, role-aware UI.

**Accept when:** a viewer can navigate every page; an operator can acknowledge an anomaly and dismiss a recommendation; an admin can add a source and trigger a sync; the assistant streams responses and shows the tool calls it used.

### Phase 8: Hardening and documentation

- Test coverage of 70% or better on `services/` and `ml/`; lint and type checks clean; Postman collection complete.
- README: what it is, an architecture diagram, a quickstart (including the model download), screenshots, the ML evaluation summary, known limitations, and the V2 roadmap.

**Accept when:** a fresh clone runs end to end following only the README.

---

## 15. Definition of Done (V1)

- [ ] `git clone`, `cp .env.example .env`, download the model, `make up`, `make seed`, `make train`: the full platform works
- [ ] Both hypervisor sources discovered and normalized
- [ ] 180 days of metrics visible in charts
- [ ] Anomalies, forecasts and recommendations generated and evaluated, with numbers in `docs/ml-evaluation.md`
- [ ] Assistant answers grounded questions using a local Hugging Face model, with eval results recorded
- [ ] All tests, lint and type checks pass
- [ ] README complete with screenshots

---

## 16. Known Risks

| Risk | Mitigation |
|---|---|
| Small local model picks wrong tools or invents arguments | Few, well-named tools with strict pydantic schemas; low temperature; validation retry; eval set to measure it |
| LSTM fails to beat the naive baseline on synthetic data | Acceptable; report it. The baseline comparison is the point. Try a smaller model or more history before adding complexity |
| vcsim object properties differ from real vCenter | Keep all pyVmomi field access inside `vsphere.py` with defensive getattr; note it in the README |
| CPU-only inference is slow | 3B fallback; streaming so the user sees progress; cap tool rounds |
| Metric volume grows quickly at 5-minute resolution | Time-series collection, TTL, and ML reading only from `metrics_daily` |