# episerve_api_server

FastAPI + NiceGUI server that is the primary user-facing entry point to the EPISERVE platform. Exposes a REST API and a web UI for browsing datasets, listing models, and triggering model runs.

GitHub: `https://github.com/The-EPISERVE-Consortium/episerve_api_server`  
Production: `https://api.episerve.zib.de`  
Interactive API docs: `https://api.episerve.zib.de/docs`

## Endpoints

All GET endpoints read from CKAN. `POST /model-runs` triggers a Prefect flow run.

`POST /model-runs` first checks `config` against the parameters the model declares in the catalog
(`additionalProperty` of the model's FDO: `valueRequired`, `minValue`, `maxValue`; a `null` value counts as
missing) and answers `422` with every violation, before any Prefect flow run is created. A model the catalog
does not know, or an unavailable catalog, is not blocked: the model container validates the same rules itself.
The UI run workflow (step 5) applies the same check. A run is registered in the catalog when it ends, so a
run that failed inside the model container (for example on its own config check) is listed with status `failed`;
only `success` runs can be added to a benchmark.

| Method | Path | Description |
|---|---|---|
| `GET` | `/health` | Health check → `{"status": "ok"}` |
| `GET` | `/datasets` | List processed FDO datasets from CKAN (`type-raw-data` group) |
| `GET` | `/models` | List forecast models from CKAN (`type-model` group) |
| `POST` | `/model-runs` | Trigger a model run via Prefect (returns `202`) |
| `GET` | `/model-runs` | List all model runs from CKAN (`type-model-run` group), including runs that ended `failed` |
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

A **benchmark** is one series: a processed dataset (`dataset_qid`), a target column, a date column and any number of equality filters (`filters`: a list of `{column, value}`, no limit through the API; the designer form offers up to five with an **Add filter** button; the first is also given as `filter_column` / `filter_value`, which older clients still use). Several filters are needed when one column does not identify a series, e.g. `Bundesland_id = 5` and `Altersgruppe = 35-59`. The series is cut into two ranges, both generated from lakeFS: the **input range** (`history_start`..`history_end`, what a model run may see; the start is optional and, if the end is left empty, the last observation before the test start is used) and the **reference range** (`test_start`..`test_end`, what forecasts are scored against). A benchmark also has a name, an optional description and the name of the input file (default `input.parquet`).

**Covariates**: a benchmark can add further data sources (`covariates`: a list of `{name, dataset_qid, date_column, columns, filters, aggregation}`; the designer has **Add covariate** with its own filters). The target series sets the time steps; the rows of a covariate between two steps are combined into the later one (`aggregation`: `mean`, `sum` or `last`), nothing is filled in. When **any** benchmark is finalized (with or without covariates), the server builds **two Parquet files** from the pinned data commit (`app/benchmarks/materialize.py`) and stores them unchanged in the lakeFS repository `benchmarks` (`LAKEFS_BENCHMARKS_REPO`) as **one FDO object with two components** (own QID and the usual layout: `pp/qq/rr/<QID>/<QID>.fdo.json` and `components/<file>`; `app/benchmarks/fdo.py`): `input.parquet` (history window only: date, target, and the covariate columns named `<name>__<column>`, other characters replaced by `_`) and `reference.parquet` (test window: date and target; never given to a model). The DOIP server serves them (its `LAKEFS_REPOS` lists `benchmarks`), the sync flow `sync_ckan_with_lakefs_benchmarks` registers them in CKAN in the group `type-benchmark-data`, and they are always open (`accessRights = open`): the check refuses a benchmark whose target or covariate dataset is `restricted`. The manifest (commit, QID, SHA-256, rows, columns) is kept in `benchmarks.materialized`. The check shows the columns and the share of empty values before finalizing. A run qualifies when it read exactly the generated input object at the stored version and nothing else (`input_sources` of the run record, taken from its provenance); the reference file is read and checked against its hash when a run is added. `run-spec` then returns the DOIP URL of the input file (`<DOIP>/doip/retrieve/<QID>/input.parquet?version=<commit>`), an empty SQL and the column names; `generated_files` of `GET /benchmarks/{id}` lists both files with their download URLs, and the Data tab shows the sources and the download links.

**Lifecycle**: `draft` -> `final` -> `retired`.

- A **draft** can be edited by logged-in users and is invisible to anonymous readers. **Check** reads the data at the current lakeFS head and reports problems (which block finalizing), warnings, the number of reference observations, the derived forecast length and the columns of the generated input file.
- **Finalize** re-checks the data and, if there are no problems, pins one lakeFS commit of the `data-processed` repository (`data_commit`, used for input and reference alike) and stores the forecast length (`prediction_length`, the number of steps of the data's frequency from the last input observation to the last reference observation) and the dataset file name (`component_name`). In the designer, finalizing is refused when the data changed since the check. A draft records its creator (`created_by`, the user's id; `Copy as draft` makes the copier the creator) and **only the creator can finalize it** (`403` otherwise); a draft made before creators were kept (no `created_by`) can be finalized by an admin. The benchmark shows the creator's display name (`created_by` in `GET /benchmarks*`, "Created by" on the Overview tab), never the email.
- A **final** benchmark is immutable. **Copy as draft** creates a new draft with the same definition; that is the way to change one.
- **Retire** hides a benchmark from the lists; its leaderboard stays readable.

**Run workflow**: on `/ui/run_workflow/1`, **Start from a benchmark** prefills the run from a final benchmark: the input URL of the generated input file (`lakefs://benchmarks/main/<id>/input.parquet?version=<commit>`), no SQL, the input file name and, for models that declare them, `prediction_length` and `prediction_offset` (`0`). Step 5 shows a warning when the current input is not that file or an SQL transformation is applied to it.

An **entry** is a model run added to a benchmark on its leaderboard page `/ui/benchmarks/{id}` (logged-in users only). The run is qualified and scored once; the metrics (MAE, RMSE, 80% coverage, 80% interval score), parameters, history window and per-date points are stored in the MariaDB database `benchmarks`, and reading a leaderboard only queries that database. Removing an entry hides it (soft delete); adding the run again recomputes it.

A run can be added only if:

- its status is `success`;
- it read exactly the benchmark's generated input file at the version stored in the benchmark (`input_sources` of the run record, taken from its DOIP provenance `prov:used`), and nothing else; a run with another version, with further inputs or without that input is refused or not listed;
- it forecasts every observed date in the reference range.

The truth is read from the benchmark's generated `reference.parquet` (checked against its SHA-256), so a later revision of the data never changes a score.

Predictions are read from the run's `predictions.tsv` CKAN DataStore resource. Coverage and interval score need the `<target>_q10` and `<target>_q90` columns; they are shown only when every scored date has both.

| Endpoint | Auth | Status | Purpose |
|---|---|---|---|
| `GET /benchmarks` | open | 200 | Final benchmarks with entry counts |
| `GET /benchmarks/drafts` | Bearer | 200 | Draft benchmarks |
| `GET /benchmarks/{id}` | open (draft: Bearer) | 200 | One benchmark in any state; drafts, including retired drafts, are hidden from anonymous readers (`404`) |
| `POST /benchmarks` | Bearer | 201 | Create a draft |
| `PATCH /benchmarks/{id}` | Bearer | 200 | Change fields of a draft (only the fields sent; `null` clears an optional one) |
| `POST /benchmarks/{id}/check` | Bearer | 200 | Check a draft against the current data; stores nothing |
| `POST /benchmarks/{id}/finalize` | Bearer (the draft's creator) | 200 | Pin the current lakeFS commit and make the draft final |
| `POST /benchmarks/{id}/copy` | Bearer | 201 | Copy a benchmark as a new draft |
| `POST /benchmarks/{id}/retire` | Bearer | 204 | Retire a benchmark |
| `GET /benchmarks/{id}/run-spec` | open | 200 | Generated input object (DOIP URL with `?version=`), empty SQL, column names, file name and `prediction_length`/`prediction_offset` of a final benchmark |
| `GET /benchmarks/{id}/leaderboard?rank_by=&all_runs=` | open | 200 | Stored ranking. `rank_by`: `mae`, `rmse`, `coverage`, `interval_score` (default); `all_runs=false` (default) shows each model's best entry, `true` every entry |
| `POST /benchmarks/{id}/entries` | Bearer | 201 | Score and add a run (`{"run_qid": ...}`) |
| `DELETE /benchmarks/{id}/entries/{entry_id}` | Bearer | 204 | Remove an entry |

Error codes: `401`/`403` missing or invalid token on a Bearer endpoint; `404` unknown benchmark, entry, run or dataset, a draft (retired drafts included) requested without a token, `run-spec` or adding an entry for a benchmark that is not final, or retiring a retired benchmark (`GET /benchmarks/{id}` and `/leaderboard` still return `200` for a retired benchmark); `409` changing, checking or finalizing a benchmark that is not a draft, a run that is already on the board; `422` invalid input, a check with problems on finalize, or a run that cannot be scored (reason in `detail`); `502` backend (database or data storage) unavailable.

Pages:

- **API box**: every UI page shows a gray box under the top bar with the curl command for what the page shows (`/datasets`, `/models`, `/model-runs`, `/benchmarks`, `/benchmarks/{id}/leaderboard`; the run workflow pages show `POST /model-runs`). The copy button adds the bearer token. The AI blackboard and the login page have none, because there is no REST endpoint behind them.
- `/ui/benchmarks`: the final benchmarks as cards (the whole card links to the benchmark's overview). A card shows the name, the description (or a sentence generated from the definition), `Dataset · target · filter`, and in a dark block on the right the date it was finalized and the number of runs; a strip below it has the test window and input range, the best entry (ranked by interval score, or by MAE if none has one), the data commit and a quick link to the leaderboard tab. Logged-in users also get the draft list, the designer (create, edit, check, finalize) and **Copy as draft** and **Retire**.
- `/ui/benchmarks/{id}`: one benchmark in three tabs (`?tab=overview` (default), `?tab=data`, `?tab=leaderboard`; the former names `get-started`, `evaluation` and `run` open the tab that now holds their content; the old `/ui/leaderboard[?benchmark=N]` redirects here). The header is the title only. **Overview**: the description (or a sentence generated from the definition) and, in a small table, the series, the forecast horizon and the number of runs. An admin can always edit the description (pencil button, or `PUT /benchmarks/{id}/description`), also of a final benchmark. **Data**: how to get the data and how forecasts are judged, and nothing more. *Training data*: the generated input file (download link, columns, sources); *Evaluation data*: the generated reference file; *Evaluation criteria*: the metrics and their direction. **Leaderboard**: the best entry, rank-by (default MAE), a show-all-runs switch (off: only the top 3 runs of the current ranking and filters), a search box (model, run QID or parameter), model and minimum-history filters and a sortable, paginated table (a tooltip on every column title explains it, e.g. MAE, RMSE, 80% coverage, interval score) with run QID (a link), date, parameters, history, the four metrics and the scored count; a model whose name contains `baseline` carries a *baseline* badge. Clicking a row opens that run in a new browser window (`/ui/benchmarks/{id}/runs/{run_qid}`, also usable as a shareable link) with the chart, the per-date table and the data pin. Logged-in users (right `benchmarks`) also get, below the table, **Potential candidates for this leaderboard**: the finished runs that fit the benchmark but are not on it yet, with model, tag, run, date, parameters and history window (no scores: those are computed when a run is added), with a checkbox per row, **Select all** and **Add selected** (the runs are scored and stored one after the other; the ones that are refused are listed with the reason) (the list is read from CKAN/DOIP after the page is shown, only for these users).

The tables `benchmarks` and `entries` are created by an administrator (no schema code). The columns of `benchmarks` that define a benchmark are `name`, `description`, `dataset_qid`, `target_column`, `date_column`, `filters` (JSON text, all equality filters as `[[column, value], ...]` in order, NULL if none; replaces the earlier columns `filter_column`, `filter_value` and `extra_filters`), `covariates` (JSON text, NULL if none; `ALTER TABLE benchmarks ADD COLUMN covariates TEXT NULL`) and, set when a benchmark with covariates is finalized, `materialized` (JSON text, the manifest of the generated files; `ALTER TABLE benchmarks ADD COLUMN materialized TEXT NULL`), `test_start`, `test_end`, `history_start`, `history_end`, `input_filename`; `created_by` (user id, set when the draft is created or copied; `ALTER TABLE benchmarks ADD COLUMN created_by INT NULL` on an existing database); finalizing sets `data_commit`, `component_name`, `prediction_length`, `truth_points` and `finalized_at`, retiring sets `retired_at`. Configuration: `BENCHMARKS_DB`, `BENCHMARKS_USER`, `BENCHMARKS_PASSWORD`; user and password default to the `BLACKBOARD_*` values. The database user needs SELECT, INSERT and UPDATE.

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

- **Authentication**: every user has a row in the `users` table (database `benchmarks`: `email`, `name`, `password_hash` (scrypt), `rights`, `active`). Log in on `/login` or `POST /auth/token` (`username` = the email) to get a personal token for the current UTC day (`<id>.<day>.<signature>`; changing the password or disabling the user kills it at once). Rights, one per feature: `runs` (`POST /model-runs`, the trigger/workflow pages), `benchmarks` (drafts and all benchmark/entry writes), `blackboard` (the AI Blackboard), `admin` (all). Catalog reads and the benchmark/leaderboard pages stay public. Manage users on the server: `kubectl -n default exec -it deploy/episerve-api-server -- python -m app.users add|list|passwd|rights|disable|enable`. `AUTH_MASTER_SECRET` and `SESSION_SECRET` must be set to strong random values — the server refuses to start on the shipped placeholder session secret.
- **Table** (hand-provisioned; the blackboard credentials need SELECT/INSERT/UPDATE on it): `CREATE TABLE users (id INT AUTO_INCREMENT PRIMARY KEY, email VARCHAR(255) NOT NULL UNIQUE, name VARCHAR(255) NOT NULL, password_hash VARCHAR(255) NOT NULL, rights VARCHAR(255) NOT NULL DEFAULT '', active TINYINT(1) NOT NULL DEFAULT 1, created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP) CHARSET utf8mb4;`
- **Tests**: `pytest tests/` runs the unit and in-memory UI tests (previews, SQL validation, secrets, benchmarks, leaderboard, workflow); no network needed.
- **CKAN plugins**: `CKAN__PLUGINS` hot-patches to `wsgi.py` persist across container restarts within the same pod — a `kubectl rollout restart` is needed to clear them.
- **WebSocket**: requires the `shared-gateway-websocket` HTTPListenerPolicy (already applied via `episerve-k8s`).

- **Mail**: `app/mailer.py` (`send_mail(to, subject, text)`) sends through AgentMail (`AGENTMAIL_API_KEY`, `AGENTMAIL_INBOX` = the sending inbox address, from the optional secret `agentmail-credentials`, keys `api-key` and `inbox`). Without them nothing is sent and the call returns False. Check with `python -m app.mailer <to> <subject> <text>` in the pod.
- **Password reset**: `/login` links to `/forgot-password` (email -> a mail with a one-hour link, always the same answer, one mail per address and minute) and `/reset-password?token=…` (new password, at least 10 characters). The link is signed with the password hash, so it works once. Links use `PUBLIC_URL` (default `https://api.episerve.zib.de`).
- **Login lock**: `app/login_guard.py` counts failed logins (`/login` and `POST /auth/token`) in memory over 15 minutes: at most 5 per IP address (whatever accounts they were for) and 30 per email (guessing spread over many IPs). At the limit the login is refused before any password is checked (`429` with `Retry-After`, a message on the page). A success clears the counter of that email but not that of the IP. The IP is the last `X-Forwarded-For` entry (the one the gateway appended). A restart resets the counters; each replica counts on its own. People behind one shared address share the 5 attempts.
- **Look of the UI pages**: `app/ui/theme.py` (applied by the page header): beige page background, the content in a white card with rounded corners, the footer in the same beige, blue accent (`ACCENT`) for links, tabs, buttons and the active menu item, navy for the other menu items. Colours are constants at the top of the file.
- **Who executed a run**: `POST /model-runs` and the trigger/workflow pages pass the pseudonym `episerve_user_<id>` (`users.pseudonym`; the id of the user in the `users` table, which only an administrator can resolve) as the flow parameter `executed_by`. The model runner records it in the run's provenance (`schema:agent`), the sync copies it to the CKAN extra `executed_by`, and `GET /model-runs` and the Model Runs page show it ("Executed by"; empty for runs from before). Neither name nor email is ever written to the metadata.

