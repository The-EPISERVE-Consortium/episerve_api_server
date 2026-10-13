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
its own catalog records and downloads its Parquet component only from the configured
DOIP service's retrieve path. Redirects are rejected. Downloads and decoded samples
are each limited to 64 MiB; queries cover the first 50,000 rows, with a visible notice
when the source has more rows. Query connections have external access and automatic
extension installation/loading disabled. Components hosted elsewhere or exceeding
these limits remain available through their download links but cannot be previewed.

Model-run previews retain their existing behavior: Parquet files use DuckDB HTTP
range requests, and CSV/TSV previews request the first 4 MiB (`?size=` on the retrieve
URL, see `app/clients/doip.py`). Their separate metadata-driven query and URL risks
(F3 in `SECURITY-SCREENING.md`) are outside this F1 change.

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

## Backend connections

| Endpoint | Backend |
|---|---|
| `GET /datasets`, `GET /models`, `GET /model-runs*` | CKAN (`ckan.ckan.svc.cluster.local` in K8s) |
| `POST /model-runs` | Prefect (`prefect-server.default.svc.cluster.local:4200` in K8s) |

## Known issues

- **Authentication**: no auth is implemented yet. The server relies on the cluster network boundary.
- **Tests**: only a `/health` smoke test exists.
- **CKAN plugins**: `CKAN__PLUGINS` hot-patches to `wsgi.py` persist across container restarts within the same pod — a `kubectl rollout restart` is needed to clear them.
- **WebSocket**: requires the `shared-gateway-websocket` HTTPListenerPolicy (already applied via `episerve-k8s`).
