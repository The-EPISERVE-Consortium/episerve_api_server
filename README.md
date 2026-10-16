# episerve_api_server

FastAPI + NiceGUI server that is the primary user-facing entry point to the EPISERVE platform. Exposes a REST API and a web UI for browsing datasets, listing models, and triggering model runs.

GitHub: `https://github.com/The-EPISERVE-Consortium/episerve_api_server`  
Production: `https://api.episerve.zib.de`  
Interactive API docs: `https://api.episerve.zib.de/docs`

## Endpoints

All GET endpoints read from CKAN. `POST /model-runs` triggers a Prefect flow run.

| Method | Path | Description |
|---|---|---|
| `GET` | `/health` | Health check → `{"status": "ok"}` |
| `GET` | `/datasets` | List processed FDO datasets from CKAN (`type-raw-data` group) |
| `GET` | `/models` | List forecast models from CKAN (`type-model` group) |
| `POST` | `/model-runs` | Trigger a model run via Prefect (returns `202`) |
| `GET` | `/model-runs` | List all model runs from CKAN (`type-model-run` group) |
| `GET` | `/model-runs/{run_id}` | Get a single run by QID (fetched from CKAN) |
| `GET` | `/ui` | NiceGUI web interface |

## Web UI

The NiceGUI interface is served at `/` (redirects to `/ui`).

| Page | URL | Description |
|---|---|---|
| Datasets – Raw | `/ui/datasets/raw` | Raw dataset list |
| Datasets – Processed | `/ui/datasets/processed` | Processed FDO datasets with DOIP links |
| Models | `/ui/models` | Registered forecast models |
| Model Runs | `/ui/model-runs` | All runs with status and provenance |
| Trigger | `/ui/trigger` | Select a dataset + model and submit a run |

The `/ui/datasets` preview uses column selection, filtering, sorting, and server-side
pagination instead of executable SQL. The server resolves the selected dataset from
its own catalog records. Use **Add filter** and **Remove filter** to combine up to
20 conditions with AND, then click **Apply**; pagination retains those applied
conditions until you apply changes. Selecting another dataset resets the filters.
The server downloads its Parquet component only from the configured
DOIP service's retrieve path. Redirects are rejected. Downloads and decoded samples
are each limited to 64 MiB; queries cover the first 50,000 rows, with a visible notice
when the source has more rows. Query connections have external access and automatic
extension installation/loading disabled. Components hosted elsewhere or exceeding
these limits remain available through their download links but cannot be previewed.

Model-run previews use the same restricted DOIP downloads and generated Parquet
queries. Metadata downloads are capped at 1 MiB, each run previews at most 20
components, and the **Max rows** control accepts 1–500 rows. CSV/TSV previews request
at most 4 MiB and reject larger responses even if the gateway ignores that request.
All preview downloads reject redirects and unapproved destinations.

Saved input components take priority over original input references. If no saved
inputs exist, original inputs are explicitly labelled as untransformed data.
Recorded transformation SQL is available as provenance only; previews never execute
it. Changing **Max rows** uses the same validated component list and input preference.

## Benchmarks and leaderboard

A **benchmark** is one series: a processed dataset (`dataset_qid`), a target column, a date column and an optional single equality filter (`filter_column` = `filter_value`). The series is cut into two ranges, both generated from lakeFS: the **input range** (`history_start`..`history_end`, what a model run may see; the start is optional and, if the end is left empty, the last observation before the test start is used) and the **reference range** (`test_start`..`test_end`, what forecasts are scored against). A benchmark also has a name, an optional description and the name of the input file (default `input.parquet`).

**Lifecycle**: `draft` -> `final` -> `retired`.

- A **draft** can be edited by logged-in users and is invisible to anonymous readers. **Check** reads the data at the current lakeFS head and reports problems (which block finalizing), warnings, the number of reference observations, the derived forecast length and the generated input SQL.
- **Finalize** re-checks the data and, if there are no problems, pins one lakeFS commit of the `data-processed` repository (`data_commit`, used for input and reference alike) and stores the forecast length (`prediction_length`, the number of steps of the data's frequency from the last input observation to the last reference observation) and the dataset file name (`component_name`). In the designer, finalizing is refused when the data changed since the check.
- A **final** benchmark is immutable. **Copy as draft** creates a new draft with the same definition; that is the way to change one.
- **Retire** hides a benchmark from the lists; its leaderboard stays readable.

**Run workflow**: on `/ui/run_workflow/1`, **Start from a benchmark** prefills the run from a final benchmark: the input URL `<DOIP>/doip/retrieve/<dataset>/<file>?version=<data_commit>`, the generated input SQL, the input file name and, for models that declare them, `prediction_length` (the benchmark's forecast length) and `prediction_offset` (`0`). Step 5 shows a warning when the current input no longer qualifies (input not read at the benchmark's commit, history window not before the test start, filter missing from the SQL).

An **entry** is a model run added to a benchmark on `/ui/leaderboard` (logged-in users only). The run is qualified and scored once; the metrics (MAE, RMSE, 80% coverage, 80% interval score), parameters, history window and per-date points are stored in the MariaDB database `benchmarks`, and reading a leaderboard only queries that database. The reference data is read from lakeFS at the benchmark's `data_commit`, from the component file the benchmark pinned, so a later revision of the data never changes a score. Removing an entry hides it (soft delete); adding the run again recomputes it.

A run can be added only if:

- its input dataset is the benchmark's dataset and its status is `success`;
- it read its input at the benchmark's commit: the run's DOIP provenance (`prov:used`) must list the dataset's retrieve URL with `?version=<data_commit>`, and exactly one input of the benchmark's dataset; a run without that version, with a different one, with several inputs of that dataset or with unreadable provenance is refused;
- its history window, read from the applied transformation SQL (also from the provenance), has a single upper date bound on the date column, written as `DATE 'YYYY-MM-DD'`, `'YYYY-MM-DD'` or with the column wrapped as `CAST(col AS DATE)`. SQL containing `OR`, `NOT`, `UNION`/`EXCEPT`/`INTERSECT` or `INTERVAL` is not scorable, also when those words appear inside string literals;
- the history ends before `test_start`;
- if the benchmark has a filter, the SQL contains it as an equality condition (`OR`, `NOT` or `CASE` anywhere in the SQL means it does not count);
- it forecasts every observed date in the reference range.

Predictions are read from the run's `predictions.tsv` CKAN DataStore resource. Coverage and interval score need the `<target>_q10` and `<target>_q90` columns; they are shown only when every scored date has both.

| Endpoint | Auth | Status | Purpose |
|---|---|---|---|
| `GET /benchmarks` | open | 200 | Final benchmarks with entry counts |
| `GET /benchmarks/drafts` | Bearer | 200 | Draft benchmarks |
| `GET /benchmarks/{id}` | open (draft: Bearer) | 200 | One benchmark in any state; drafts, including retired drafts, are hidden from anonymous readers (`404`) |
| `POST /benchmarks` | Bearer | 201 | Create a draft |
| `PATCH /benchmarks/{id}` | Bearer | 200 | Change fields of a draft (only the fields sent; `null` clears an optional one) |
| `POST /benchmarks/{id}/check` | Bearer | 200 | Check a draft against the current data; stores nothing |
| `POST /benchmarks/{id}/finalize` | Bearer | 200 | Pin the current lakeFS commit and make the draft final |
| `POST /benchmarks/{id}/copy` | Bearer | 201 | Copy a benchmark as a new draft |
| `POST /benchmarks/{id}/retire` | Bearer | 204 | Retire a benchmark |
| `GET /benchmarks/{id}/run-spec` | open | 200 | Input URL with `?version=`, SQL, file name and `prediction_length`/`prediction_offset` of a final benchmark |
| `GET /benchmarks/{id}/leaderboard?rank_by=&all_runs=` | open | 200 | Stored ranking. `rank_by`: `mae`, `rmse`, `coverage`, `interval_score` (default); `all_runs=false` (default) shows each model's best entry, `true` every entry |
| `POST /benchmarks/{id}/entries` | Bearer | 201 | Score and add a run (`{"run_qid": ...}`) |
| `DELETE /benchmarks/{id}/entries/{entry_id}` | Bearer | 204 | Remove an entry |

Error codes: `401`/`403` missing or invalid token on a Bearer endpoint; `404` unknown benchmark, entry, run or dataset, a draft (retired drafts included) requested without a token, `run-spec` or adding an entry for a benchmark that is not final, or retiring a retired benchmark (`GET /benchmarks/{id}` and `/leaderboard` still return `200` for a retired benchmark); `409` changing, checking or finalizing a benchmark that is not a draft, a run that is already on the board; `422` invalid input, a check with problems on finalize, or a run that cannot be scored (reason in `detail`); `502` backend (database or data storage) unavailable.

Pages:

- `/ui/benchmarks`: the list of final benchmarks (with a link to each leaderboard) for everyone; logged-in users also get the draft list, the designer (create, edit, check, finalize) and **Copy as draft** and **Retire**.
- `/ui/leaderboard`: a benchmark selector, rank-by, a show-all-runs switch and a sortable table with model and minimum-history filters. Clicking a row opens a detail dialog with the chart, the per-date table and the data pin. Logged-in users also get **Add run** and **Remove**.

The tables `benchmarks` and `entries` are created by an administrator (no schema code). The columns of `benchmarks` that define a benchmark are `name`, `description`, `dataset_qid`, `target_column`, `date_column`, `filter_column`, `filter_value`, `test_start`, `test_end`, `history_start`, `history_end`, `input_filename`; finalizing sets `data_commit`, `component_name`, `prediction_length`, `truth_points` and `finalized_at`, retiring sets `retired_at`. Configuration: `BENCHMARKS_DB`, `BENCHMARKS_USER`, `BENCHMARKS_PASSWORD`; user and password default to the `BLACKBOARD_*` values. The database user needs SELECT, INSERT and UPDATE.

## Running locally

```bash
cp .env.example .env        # fill in PREFECT_API_URL at minimum
./run_local.sh              # creates venv, pulls K8s secrets, starts server
```

Server starts at `http://localhost:8000` (override with `PORT=8009 ./run_local.sh`).

**Prefect for local use** — set `PREFECT_API_URL` to either:
- `https://prefect.episerve.zib.de/api` (external)
- `http://localhost:4200/api` after `kubectl port-forward svc/prefect-server 4200:4200`

> If `http_proxy` is set in your environment, prefix curl commands with `no_proxy=localhost,127.0.0.1`.

## Configuration

All configuration via environment variables. In K8s these come from sealed secrets. Locally, `run_local.sh` pulls secrets from the cluster and loads the rest from `.env`.

| Variable | Source | Default |
|---|---|---|
| `PORT` | `.env` | `8000` |
| `LAKEFS_URL` | `.env` | `https://lake-episerve.zib.de` |
| `LAKEFS_RAW_REPO` | `.env` | `data-raw` |
| `LAKEFS_PROCESSED_REPO` | `.env` | `data-processed` |
| `LAKEFS_MODEL_RUNS_REPO` | `.env` | `model-runs` |
| `LAKEFS_BRANCH` | `.env` | `main` |
| `LAKEFS_ACCESS_KEY` | K8s secret `lakefs-credentials` | — |
| `LAKEFS_SECRET_KEY` | K8s secret `lakefs-credentials` | — |
| `CKAN_URL` | `.env` | `https://data.episerve.zib.de` |
| `CKAN_API_TOKEN` | K8s secret `ckan-credentials` | — |
| `PREFECT_API_URL` | `.env` | — |
| `PREFECT_API_KEY` | `.env` | — |
| `PREFECT_MODEL_RUNNER_DEPLOYMENT` | `.env` | `model-pipeline/model-runner` |
| `DOIP_URL` | `.env` | `https://doip.episerve.zib.de` |
| `SESSION_SECRET` | K8s sealed secret | — (required; the server refuses to start while unset or still the shipped default) |

## Backend connections

| Endpoint | Backend |
|---|---|
| `GET /datasets`, `GET /models`, `GET /model-runs*` | CKAN (`ckan.ckan.svc.cluster.local` in K8s) |
| `POST /model-runs` | Prefect (`prefect-server.default.svc.cluster.local:4200` in K8s) |

## Known issues

- **Authentication**: a single shared daily token (derived from `AUTH_MASTER_SECRET`) gates `POST /model-runs` and the blackboard/workflow UI pages; catalog reads are public. `AUTH_MASTER_SECRET` and `SESSION_SECRET` must be set to strong random values — the server now refuses to start on the shipped placeholder session secret.
- **Tests**: only a `/health` smoke test exists.
- **CKAN plugins**: `CKAN__PLUGINS` hot-patches to `wsgi.py` persist across container restarts within the same pod — a `kubectl rollout restart` is needed to clear them.
- **WebSocket**: requires the `shared-gateway-websocket` HTTPListenerPolicy (already applied via `episerve-k8s`).
