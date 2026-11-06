# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

Everything runs through Docker Compose; the Makefile is the entry point. Host
ports are all overridable in `.env` (the collector defaults to **5001**, not
5000, because macOS AirPlay owns 5000).

```bash
make up                 # build + start (PROFILE=llm to include the local model)
make down / make reset  # stop / stop and delete data volumes
make logs SVC=api       # tail one service
make seed               # 180 days of synthetic metrics + rollup (DAYS=, INTERVAL=)
make train              # Isolation Forest + LSTM + generate findings
make evaluate           # rewrite docs/ml-evaluation.md from measurements
make assistant-eval     # score assistant tool selection (needs the llm profile)
make types              # regenerate frontend API types from the live OpenAPI doc
```

### Tests

```bash
make test                                                   # all suites
docker compose run --rm api pytest -q                       # API (needs PG + Mongo)
docker compose run --rm api pytest tests/test_ml_models.py -q -k rightsizing
docker compose run --rm --no-deps collector pytest -q       # collector (no deps)
cd frontend && npx vitest run                               # frontend
docker compose run --rm api pytest -q --cov=app --cov=ml     # coverage (~80%)
```

The API suite creates and drops its own Postgres database and Mongo database per
run, so it never touches the stack's real data.

### Lint and format

The images do **not** mount the source, so linting must be pointed at the
working tree or the fixes are written to a discarded layer:

```bash
make lint   # ruff + mypy, then eslint + tsc
make fmt    # ruff format + --fix, then prettier
```

Both are currently clean; keep them that way.

## Architecture

Read [`docs/architecture.md`](docs/architecture.md) first — it records *why*
things are the way they are. The brief being implemented is
[`spec/01-implementation.md`](spec/01-implementation.md), and section numbers
from it are cited throughout the code.

### The service boundary that matters most

**The collector never touches a database.** It normalises what it finds and
POSTs it to the API's `/internal/*` endpoints with an `X-Service-Token`.
Everything that writes to Postgres or Mongo lives in
`services/api/app/services/`. When adding a field, the change is: collector
schema → API ingest schema → SQLAlchemy model → Alembic migration → service.

`services/collector/app/schemas.py` and
`services/api/app/schemas/ingest.py` are **deliberate duplicates** of the same
contract. `services/collector/tests/test_schema_parity.py` keeps them honest.

### Where to put things

| Layer | Rule |
|---|---|
| `app/routes/*.py` | One router per resource. HTTP concerns only; no business logic |
| `app/services/*.py` | All business logic. **No FastAPI imports** — the assistant's tools reuse these directly |
| `app/db/models/` | One module per table family; enum type objects live in `types.py` |
| `ml/` | Models and batch jobs. `ml/jobs.py` is the only part that touches the database |
| `app/assistant/` | `tools.py` (registry), `loop.py` (tool rounds), `prompts.py`, `llm_client.py` |

### Non-obvious invariants

- **SQLAlchemy enums need `values_callable`.** Without it SQLAlchemy stores the
  member *name* (`"ADMIN"`) while Postgres holds the *value* (`"admin"`) and
  every insert fails. All enum types are built by `_enum()` in
  `app/db/models/types.py` — use it rather than constructing `SAEnum` inline.
- **Emails are validated by `app/schemas/common.py::Email`, not `EmailStr`.**
  Pydantic's `EmailStr` rejects `.local`, and the seeded admin is
  `admin@hvops.local`.
- **VMs are never hard-deleted.** Missing from a full batch sets
  `is_deleted = true`. An *empty* batch means "nothing collected" and must not
  soft-delete anything.
- **Audit rows join the caller's transaction.** `audit.record()` only adds to
  the session; the route commits. The exception is refresh-token family
  revocation, which commits inside the service because it then raises.
- **The synthetic generator is a function of absolute time**, not of the range
  being generated: anomaly windows come from a fixed epoch, noise from a
  counter-based RNG advanced to the point's grid index, and timestamps snap to
  the interval grid. Breaking any of these makes live-collected data disagree
  with the backfill it is appended to, and silently invalidates the ML
  evaluation.
- **ML reads only `metrics_daily`**, never raw `metrics` — except anomaly
  detection, which builds hourly windows from raw points.
- **Anomaly features are entity-relative** (deviation from the same hour on
  comparable days, weekday vs weekend kept separate). Absolute magnitudes make
  the model learn "busy is weird"; see `ml/data.py` for the measured history.
- **The LSTM predicts the change from the last observed value**, not the level,
  so a growing series can be forecast above its own training range.
- **Which forecast the UI leads with is measured, not assumed** —
  `MLState.preferred_forecast_model` reads the stored holdout result.
- **The frontend's API types are generated.** `src/api/schema.d.ts` comes from
  the OpenAPI document; `src/api/types.ts` is only aliases into it. Change a
  response model, then run `make types`.
- **The collector runs one gunicorn worker on purpose.** APScheduler is
  in-process; two workers would double every poll.

### ML artifacts

Training writes to `ML_ARTIFACT_DIR` (the `./ml_artifacts` volume, shared with
the collector so `generate_history` can drop `ground_truth.json` where
`ml.evaluate` finds it). The API loads artifacts at startup and on
`POST /ml/reload`, so retraining never needs a restart.

`docs/ml-evaluation.md` is **generated** by `make evaluate` — edit
`ml/evaluate.py`, not the markdown.

### Honest-reporting expectations

The spec requires measured results, including unflattering ones, and the code
and docs follow that. Two currently stand: the LSTM loses to the seasonal-naive
baseline overall (so the baseline ships as the default), and the anomaly
detector's F1 is carried by the minimum-deviation gate rather than the Isolation
Forest. Both are stated in `docs/ml-evaluation.md` with the ablation. If a
change improves or worsens either, re-run `make evaluate` and update the README
summary to match — do not leave a stale number in place.
