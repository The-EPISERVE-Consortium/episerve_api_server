# Input & Forecast previews

`GET /model-runs/{qid}/forecast-preview?version=<full-run-commit>` reconstructs
**input supplied** by replaying recorded workflow SQL and reads the existing
`predictions.tsv`. Omitting `version` resolves the latest object version first.
It never reruns a model or infers its internal history selection. No lakeFS writes
or additional model metadata are required.

## Database setup

Before deploying, apply `migrations/20261111_add_model_run_preview_cache.sql` to
the configured `BENCHMARKS_DB` as its administrator. Grant the existing API account
SELECT, INSERT and UPDATE on `model_run_preview_cache`; no runtime DDL or DELETE
is required. The migration contains a grant example; substitute the actual user,
account host and database. Existing credentials and `app.db.connect()` are reused.

A unique `(run_qid, artifact_commit, format_version)` identifies each JSON preview.
Only opening the CKAN tab requests it. A cache miss acquires a MariaDB GET_LOCK,
rechecks the cache, then builds and stores the result. Other processes receive a
retryable HTTP 503 while it is being generated. Locks release even after failures.

## Access and limits

The endpoint only exposes public runs and inputs. Current access is checked on
**every** request, before reading cached data; restricted runs or inputs return
403. CKAN hides the tab for anonymous visitors to restricted entries. Authenticated
restricted previews are not supported by this public endpoint. Responses use
`Cache-Control: no-store`; only the server database caches verified public data.

Source URLs must use the configured DOIP origin and a full version pin. An older
`lakefs://<benchmarks-repo>/<ref>/<numeric-id>/input.parquet?version=...` is read only
when that benchmark is public. Other internal URI shapes are rejected.

Downloads, rows, columns, series, SQL time/memory, chart points and cache JSON size
are bounded. Read-only SQL executes without external access. Large series show an
explicit evenly spaced display sample; original downloads remain unchanged.
Categorical columns form separate series; multiple possible time axes or duplicate
axis values are shown as tables rather than connected misleadingly. Quantiles are
labelled "reported q10–q90", without asserting their statistical interpretation.
Input and prediction axes are kept separate when their correspondence is unknown.

If supplied input is unavailable, predictions remain viewable with an explanation.
Incomplete previews are not stored as successful cache entries, so a later visit
can retry. Failed runs without predictions show an unavailable message.

## Rebuilding and cleanup

Immutable previews have no TTL. Changing `FORMAT_VERSION` creates a new namespace
when extraction rules change. Administrators can remove disposable cache entries
using credentials with DELETE permission (never grant this just for runtime use):

```bash
python -m app.forecast_preview.manage --run Q1793997474911
python -m app.forecast_preview.manage --before 2026-01-01
```

The next tab opening regenerates a removed preview. Cleanup leaves original
artifacts and other database tables untouched.
