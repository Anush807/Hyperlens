# Architecture

How HVOps is put together, and why. The what-to-build brief is
[`spec/01-implementation.md`](../spec/01-implementation.md); this is the record
of the decisions taken while building it.

---

## 1. Services

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

| Service | Stack | Owns |
|---|---|---|
| `api` | FastAPI, SQLAlchemy 2 async, Motor | Both databases, all business logic, ML inference, the assistant |
| `collector` | Flask, APScheduler, pyVmomi | Talking to hypervisors. No database access at all |
| `nutanix-mock` | FastAPI | Prism Central v3 list endpoints, served from fixtures |
| `vcsim` | govmomi simulator | A real vSphere API surface without a lab |
| `llm` | llama.cpp server | An OpenAI-compatible endpoint in front of a GGUF model |
| `frontend` | Vite, React 18, nginx | The dashboard |

### One writer per datastore

The collector never opens a database connection. It normalises what it finds
and POSTs it to `/internal/inventory` and `/internal/metrics`, authenticated
with `X-Service-Token`. Everything that writes to Postgres or Mongo lives in
`services/api/app/services/`.

The cost is one HTTP hop per batch. What it buys is that schema changes are a
one-service change, the upsert and soft-delete rules exist in exactly one place,
and the collector can be restarted, scaled or rewritten without a migration.

---

## 2. Data

### Postgres - things with identity

Inventory, users, sources, ML output, audit. Every inventory table is keyed
`UNIQUE(source_id, external_id)` and written with `INSERT ... ON CONFLICT DO
UPDATE`, so re-ingesting an identical batch changes nothing but `last_seen_at`.

VMs are **never deleted**. A VM missing from a full batch is marked
`is_deleted = true`, which keeps its metrics, anomalies and recommendations
queryable. An *empty* batch is treated as "nothing collected", not "everything
is gone", so a failed poll cannot wipe the inventory view.

### Mongo - things with a timestamp

- `metrics` — a native time-series collection, `timeField: ts`,
  `metaField: meta`, minute granularity, 180-day TTL. ~4.9M points for a
  180-day backfill of ~95 entities at 5-minute resolution.
- `metrics_daily` — pre-aggregated per entity per day (mean, p95, max), unique
  on `(entity_type, entity_external_id, date)`. **All ML reads come from here**,
  which is what keeps training time independent of raw metric volume.
- `events` — collector runs and ingest errors, 30-day TTL.

The rollup is idempotent: re-running it upserts the same rows, so a replayed
metric batch cannot skew a mean.

---

## 3. Decisions worth explaining

### Why the collector re-reads its sources every run

`GET /internal/sources` is called at the start of every poll rather than cached.
Adding a source in the UI is then picked up within one cycle with no restart,
and a disabled source stops being polled immediately. It costs one request per
cycle.

### Why each source is its own failure domain

One source failing must not stop the others — a Nutanix outage should not
freeze vSphere inventory. Each source is collected inside its own try/except,
reports its own status to `/internal/sources/{id}/status`, and the run
continues. This is asserted directly in
`services/collector/tests/test_collection.py`.

### Why metrics are synthetic

vcsim's performance counters are not realistic, and the point of the metric
pipeline is to have data worth running ML against. `MetricsProvider` is an
interface; V1 ships `SyntheticMetricsProvider` and a real vSphere or Prism
provider drops in behind the same two methods.

The generator is **deterministic from absolute time**, which took two attempts
to get right and matters more than it sounds:

- Anomaly windows are positioned from a fixed epoch, not from "now". The first
  version anchored them to the end of whatever range was being generated, so a
  180-day backfill and a 60-second live poll injected anomalies at *different*
  absolute times — phantom anomalies that no ground-truth file recorded. They
  accounted for 100 of 103 false positives before the fix.
- Noise comes from a counter-based RNG fast-forwarded to the point's absolute
  grid index, so a timestamp's value does not depend on how many points preceded
  it in the same call.
- Points land on the interval grid, so a replayed batch is a duplicate rather
  than a slightly different sample.

### Why the frontend's types are generated

`src/api/schema.d.ts` comes from the live OpenAPI document
(`npm run gen:types`); `src/api/types.ts` is nothing but aliases into it. A
response model change therefore breaks `tsc`, rather than being discovered in a
browser.

### Why the access token lives in memory

The access token is held in a Zustand store and never written to
`localStorage`; the refresh token is an httpOnly cookie the JavaScript cannot
read. On a cold load the app tries one silent refresh before deciding the user
is signed out. Concurrent 401s share a single in-flight refresh — rotation
invalidates the previous token, so three parallel refreshes would log the user
out.

---

## 4. The ML pipeline

```
metrics (raw, 5-min)
   |  hourly rollup (scheduled)
   v
metrics_daily  ──────────────► forecasting (LSTM + seasonal naive) ──► forecasts
   |                                                                      |
   └────────────────────────► right-sizing rules ──► recommendations      |
                                                                          v
metrics (raw) ──► hourly feature windows ──► Isolation Forest ──► anomalies
```

Training is a CLI inside the API image (`python -m ml.train ...`) writing
artifacts to a shared volume. The API loads them at startup and on
`POST /ml/reload`, so retraining never needs a restart.

### Anomaly detection

Every feature is **relative to the entity's own recent behaviour**, measured
against the same hour on previous days of the same type (weekday vs weekend).
That last distinction is not a refinement: these VMs run at ~55% of weekday load
at the weekend, so a baseline averaging the previous seven calendar days made
every Saturday hour look anomalous.

The detector is the spec's Isolation Forest **and** a minimum-deviation gate. The
measured ablation is in [`ml-evaluation.md`](ml-evaluation.md), and it is not
flattering to the forest on synthetic data — which is the honest result, and is
reported rather than buried.

### Forecasting

The LSTM predicts the **change from the last observed value**, not the level.
With levels, a min-max scaler fitted on the training window caps the model at
the highest value it has ever seen, so a steadily growing VM — the exact case
forecasting exists for — could never be predicted above its own history. That
one change took `growing`-profile MAE from 14.4 to 5.0.

Both models are always trained, always stored and always rendered. Which one the
UI leads with is read from the measured holdout, not assumed: see
`MLState.preferred_forecast_model`.

### Right-sizing

Deliberately rules, not a model. An operator has to act on these, and "p95 CPU
was 12% for 14 days" is an argument a human can check. The numbers that fired
each rule are stored in `evidence`.

---

## 5. The assistant

Tools call the service layer directly rather than looping back through HTTP:
fewer moving parts, and the calling user's role travels with the context. Every
tool is read-only.

The loop resolves tool calls non-streaming (the message has to be parsed for
tool calls anyway), capped at four rounds, then streams the final answer over
SSE. Invalid arguments are fed back to the model once; a second failure is
reported rather than retried forever.

Tool output is compact on purpose — truncated lists, floats to one decimal —
because the context window is 8k tokens. `get_capacity_forecast` samples five
points out of thirty for the same reason.

If the model is unreachable the endpoint returns 503 with the command to start
it. Anomaly summarisation degrades silently to `null`: it must never block
anomaly creation.

---

## 6. Request lifecycle

1. `RequestIdMiddleware` accepts or mints `X-Request-ID` and logs method, path,
   status and duration as JSON.
2. CORS is restricted to the configured frontend origins.
3. The route's dependencies resolve the session, the current user and the role
   guard (`admin > operator > viewer`).
4. The handler calls into `app/services/`, which contains no FastAPI imports and
   is what the assistant's tools reuse.
5. Mutating endpoints add an `audit_log` row **to the caller's transaction**, so
   the audit entry and the change commit or roll back together.
6. Errors leave as `{"error": {"code", "message", "details"}}`.

---

## 7. Scheduled work

| Where | When | What |
|---|---|---|
| collector | every `POLL_INVENTORY_SECONDS` | Walk every enabled source, POST inventory |
| collector | every `POLL_METRICS_SECONDS` | Generate and POST metric points |
| api | hourly, :05 | Roll `metrics` into `metrics_daily` |
| api | hourly, :15 | Anomaly detection over the last 24h |
| api | daily, 02:00 | Regenerate forecasts and recommendations |

The anomaly job runs ten minutes after the rollup so it reads an aggregation
that has finished. The collector runs a single gunicorn worker on purpose:
APScheduler is in-process, and two workers would double every poll.

---

## 8. What is deliberately not here

- **Write actions.** Nothing powers on, resizes or migrates anything. The
  assistant says so when asked.
- **Multi-tenancy.** One estate, one set of roles.
- **A real vCenter or Prism.** vcsim and the mock speak the real protocols, so
  the adapter code is real; the endpoints are not.
- **Redis.** The one cached endpoint uses a 60-second in-process TTL cache. With
  a single API replica, a cold cache costs one query.
