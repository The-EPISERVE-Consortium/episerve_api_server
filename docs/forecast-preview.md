# Input & Forecast previews

`GET /model-runs/{qid}/forecast-preview?version=<full-run-commit>` reconstructs
**input supplied** by replaying recorded workflow SQL and reads the existing
`predictions.tsv`. Omitting `version` resolves the latest object version first.
It never reruns a model or infers its internal history selection. No lakeFS writes
or additional model metadata are required.

When the supplied component is explicitly identified as benchmark input in its
pinned FDO, the preview also reads the declared `reference.parquet` from that same
object and commit. Its recorded SHA-256 is checked when present. These observed
values are shown separately from model input and never passed through the input
SQL.
Reference retrieval failures leave the input/prediction chart usable and retryable.
Current and pinned object access checks also protect the reference before cache
lookup.

For ordinary inputs, the same pinned source component is read once and reused to
recover observations at forecast dates. Simple row-wise SELECTs retain column
expressions, aliases, population filters and ordering; calendar predicates on the
unambiguous time column and LIMIT/OFFSET are removed using DuckDB's SQL syntax
tree. Input SQL and supplied-input reconstruction remain unchanged. No regular
expression is used to rewrite SQL. DuckDB is pinned to the tested version because
its serialized syntax-tree format can change between releases. Forecasts with
row-position axes do not acquire inferred dates. Only matching target columns and calendar coordinates are shown,
and no later source version is consulted.

Ambiguous queries (including joins, CTEs, aggregates, windows, mixed date/population
OR predicates, and unsupported functions) retain inputs/predictions with an
observation explanation instead of guessing. Missing forecast dates in the source
produce no overlay. Explicit benchmark reference data take precedence.
If automatic mapping fails, the UI offers a recorded-file selector and explicit
X/Y column selectors. Each file has an aligned sample of up to 1,500 rows covering
the full row range, bounded further by 12,000 cells and 192 KiB. Y choices are
numeric columns (including entirely numeric text); X may be calendar, numeric or
categorical. Samples retain original row numbers and flag shortened text labels.
Duplicate X values are detected on the complete source column, so sampling does
not hide ambiguity: repeated coordinates are plotted as separate points, without
aggregation or joining different groups. Missing cells remain gaps.

Readable files with unsupported automatic mapping are cacheable; actual retrieval
or reconstruction failures remain retryable. Format version 5 rebuilds previews
with the manual-selection samples. Selecting columns never reruns a model or
changes its recorded input SQL, and requires no additional download or endpoint.

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
Categorical columns form separate series; ambiguous automatic mappings offer
manual X/Y selection instead of connecting rows misleadingly. Quantiles are
labelled "reported q10–q90", without asserting their statistical interpretation.
The chart overlays matching series on compatible date, ISO-week, or numeric axes.
Explicit x_auto_converted positions align to supplied input row positions. When
axes cannot be aligned reliably, the preview explains why the combined graph is
unavailable and offers manual plotting of individual files. No model-internal history is inferred.

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
