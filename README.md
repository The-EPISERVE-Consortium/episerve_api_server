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
