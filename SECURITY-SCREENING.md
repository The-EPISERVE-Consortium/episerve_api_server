# OrcaCyber Zero security/vulnerability screening

> Independent code-only review: see section 8 for the verdict and corrections.
> Earlier runtime and production claims are historical assertions from the original
> report and were not independently verified in this review.

- Date: 2026-10-13
- Model: `orca/orcacyber-zero-1.5` (via provider `orcarouter`)
- Scope of this document: setup, methodology, the analysis performed, and all findings.

## 1. Setup

The screening runs inside `pi`, already wired to OrcaCyber Zero. Verified environment:

| Item | Value |
|---|---|
| Model | `orca/orcacyber-zero-1.5` (1M context, reasoning, tools) |
| Provider | `orcarouter` → `https://api.orcarouter.ai/v1` (endpoint verified live, HTTP 200) |
| Local tools | `read`, `bash`, `edit`, `write`, `web_search`, `source_check`, `fetch_content`, `get_search_content` |
| Local binaries | `rg`, `fd` (in `~/.pi/agent/bin`, on PATH), `docker`, `python3.12`, `node22` |
| Security scanners | none pre-installed (no semgrep/gitleaks/trivy/osv-scanner); Docker can supply them |
| Live-verification tooling | Playwright (python) + system Chromium at `/usr/bin/chromium` for headless UI driving |
| Repos on disk | ~27 independent git repos; `episerve/` and `mardi/` are monorepos of subrepos |

### Artifacts created for this workflow

| Path | Purpose |
|---|---|
| `~/.agents/skills/security-audit/SKILL.md` | Methodology skill: attacker control → invariant → primitive → path → boundary → impact; CONFIRMED/LIKELY/HYPOTHESIS/UNKNOWN classification. Auto-discovered by pi. |
| `~/.agents/skills/security-audit/references/report-format.md` | Fixed report schema for comparable runs. |
| `sec-scan/run-scan.sh` | Batch driver: optional dockerized scanners, then `pi --model orca/orcacyber-zero-1.5 --thinking high --skill security-audit --print` per repo. |
| `~/.pi/agent/prompts/audit.md` | `/audit [focus]` prompt template for interactive use. |
| `sec-reports/<repo>-<timestamp>/security-audit.md` | Raw per-repo report output. |

Skill discovery was verified — `pi` reports: `find-skills, remotion-best-practices, security-audit`.

### How to run

```bash
sec-scan/run-scan.sh /home/tim/episerve/episerve_api_server
sec-scan/run-scan.sh /home/tim/episerve/*          # each subrepo → own report
SCANNERS=1 sec-scan/run-scan.sh <repo>             # add gitleaks + trivy leads
```

Run inside a container for untrusted repos: the agent holds `bash`, and `setup.py` /
`postinstall` / Makefile targets are code execution from a possibly-adversarial repo.

## 2. Analysis performed

Target: `episerve/episerve_api_server` @ `47a77eab6b7f7d792dbc0a81f56641b62c48708a`
(working tree clean; 492 MB incl. venv). FastAPI + NiceGUI service fronting the EPISERVE
platform: reads dataset/model/run metadata from CKAN and lakeFS (via DOIP), renders dataset
previews by querying Parquet through DuckDB, and triggers Prefect model runs.

Trust boundaries:
1. Public HTTP/WebSocket surface (`/`, `/ui/*`, REST routers) — anonymous browsing.
2. Authenticated surface (HTTP bearer daily token, NiceGUI login session) — model-run
   triggers and AI Blackboard.
3. Backend credentials held server-side only (CKAN, lakeFS, Prefect, MariaDB).

Latest raw report: `sec-reports/episerve_api_server-20261013-115425/security-audit.md`
(updated with live confirmation).

Live re-check: F1 was independently confirmed against production on 2026-10-13
(see section 7).

## 3. Findings

| ID | Title | Severity | Classification |
|---|---|---|---|
| F1 | Unauthenticated arbitrary DuckDB SQL on `/ui/datasets` → server file read/write | P1 | CONFIRMED (live 2026-10-13) |
| F2 | `AUTH_MASTER_SECRET`/`STORAGE_SECRET` fall back to published constants | P1 | LIKELY |
| F3 | Untrusted catalog metadata interpolated into DuckDB SQL / fetched as URLs | P2 | LIKELY |
| F4 | Shared, user-independent deterministic daily bearer token | P2 | LIKELY |

### F1 — Unauthenticated arbitrary DuckDB SQL → server file read/write — P1 — CONFIRMED

- **Location:** `app/ui/pages.py:338` (`_duckdb_query`), `:505` (`run_sql`), reachable at
  `:258` (`@ui.page("/ui/datasets")`, no `_require_login`).
- **Attacker position:** Any client that can reach the web UI. No account, token, or login.
  Select any dataset row, which reveals the SQL box, then submit arbitrary SQL.
- **Violated invariant:** Untrusted web input must not reach a database engine with
  filesystem/network access in the server process; anonymous browsing must not grant
  server-side file primitives.
- **Path:** `GET /ui/datasets` renders with no login check → row selection unhides SQL box
  → `run_sql` passes input verbatim → `_duckdb_query` does `duckdb.connect()` then
  `conn.execute(sql).df()` → result returned in the HTTP response (exfiltration).
- **Boundary crossed:** Network (anonymous) → server process → host filesystem.
- **Impact:** Read any file the process can read (K8s secrets, `/proc/self/environ`, `.env`),
  write arbitrary files. App runs as root (`Dockerfile` has no `USER`). RCE plausible via
  DuckDB extension loading; SSRF to internal services via httpfs.
- **Independent verification by the reviewer (not the model):**
  - `_require_login(` has 8 call sites, none at the `/ui/datasets` block (line 258).
  - No global middleware and no `Depends(require_auth)` on the UI (`main.py`).
  - Reproduced the primitive locally on duckdb 1.5.3: `enable_external_access` defaults
    `True`; read `/etc/hostname` and `/etc/passwd` (23 rows); `COPY ... TO '/tmp/...'` wrote
    a file successfully.
- **Live confirmation (production, 2026-10-13):** Confirmed on the deployed instance
  `https://api.episerve.zib.de` by driving `/ui/datasets` anonymously — see section 7.
- **Falsifier checked:** If the page called `_require_login` or the connection set
  `enable_external_access=false`, this would downgrade. Neither is present.
- **Remediation:** Do not execute client SQL. Require auth; open the connection with
  `config={"enable_external_access": "false"}`; parse/allowlist a single `SELECT` over the
  `df` view; run queries sandboxed without secret mounts; add `USER` to the Dockerfile.

### F2 — Process-wide secrets fall back to published constants — P1 — LIKELY

- **Location:** `app/config.py:50` (`auth_master_secret` default
  `episerve-dev-secret-change-me`), `app/main.py:257` (`storage_secret` same default); used
  by `app/auth.py:14` (`_daily_token`) and NiceGUI session signing.
- **Attacker position:** Network access, plus the deployment using defaults
  (`AUTH_MASTER_SECRET`/`STORAGE_SECRET` unset). `.env.example` omits both, so following
  the example silently uses the defaults.
- **Violated invariant:** Auth material must not be derivable from public constants;
  session-signing keys must be unique per deployment.
- **Path:** If defaults apply, attacker computes
  `HMAC-SHA256("episerve-dev-secret-change-me", <today ISO date>)` = today's bearer token,
  then calls `POST /model-runs` and logs into `/ui/blackboard`; independently forges the
  signed NiceGUI `storage.user` cookie using the same public `STORAGE_SECRET`.
- **Boundary crossed:** Network → authenticated API/UI boundary.
- **Impact:** Trigger arbitrary Prefect model runs (attacker-chosen container image/tag in
  the cluster); read/modify AI Blackboard tasks and routing rules; full auth-boundary loss.
- **Disproof check:** No deployment manifest, `.env`, or startup validation forces these
  variables; `run_local.sh` exports only lakeFS/CKAN/blackboard secrets. A production env
  setting both would eliminate F2 (F1 stands regardless).
- **Remediation:** Inject strong random values; fail closed at startup if unset or equal to
  the placeholder; add both to `.env.example`; replace the shared daily token with
  per-user signed tokens with expiry/revocation.

### F3 — Untrusted catalog metadata interpolated into SQL / fetched as URLs — P2 — LIKELY

- **Location:** `app/ui/pages.py:867-877` (`_fetch_preview`: `schema:query` executed as
  subquery, `url` interpolated into `read_parquet('{url}')`), `:957-968` and `:1151-1160`
  (`prov:used` `@id` passed to fetch/query path), `:850-852` (`requests.get(url)` on
  `doip_url`).
- **Attacker position:** Ability to write FDO metadata / component URLs read by the service
  (CKAN packages/resources, lakeFS objects, DOIP responses) — a different boundary from F1.
- **Violated invariant:** Backend catalog data is untrusted and must not be concatenated
  into SQL or fetched without URL validation.
- **Path:** A `'` in the URL breaks out of the string literal in
  `CREATE VIEW df AS SELECT * FROM read_parquet('<url>')`; `schema:query` runs verbatim as a
  subquery; `@id` is fetched by DuckDB httpfs or `requests.get` (SSRF).
- **Boundary crossed:** Backend data store → server process (SQL execution, SSRF).
- **Impact:** Arbitrary DuckDB SQL and internal SSRF once catalog metadata is influenced;
  persisted injection path affecting any future preview flow.
- **Disproof check:** No URL allowlist or SQL parameterization exists in the preview
  helpers; not anonymously reachable like F1, so kept at P2.
- **Remediation:** Quote/escape `url`; never execute `schema:query` inside an interpolated
  string; restrict fetched URLs to the DOIP host (allowlist + scheme check).

### F4 — Shared, user-independent daily bearer token — P2 — LIKELY

- **Location:** `app/auth.py:14-21`, `app/routers/auth.py:36-49`.
- **Attacker position:** Anyone who obtains the token once (shown in the UI's "Show current
  token" dialog and copied into `curl` commands, `app/ui/pages.py:44-81`) or observes it in
  transit.
- **Violated invariant:** Bearer credentials should be per-principal, expiring, revocable.
- **Path:** One token per calendar day for all users, expiring only at UTC midnight; no
  rotation/revocation; `/auth/status` returns `valid=True` unconditionally regardless of the
  supplied token (`app/routers/auth.py:46`).
- **Boundary crossed:** Network → authenticated boundary.
- **Impact:** Token possession grants full authenticated access until midnight with no
  revocation; `/auth/status` gives a misleading positive validity signal.
- **Disproof check:** No per-user state, session store, or revocation list exists.
- **Remediation:** Issue per-user signed tokens (`sub`/`exp`/`jti`), verify expiry and
  revocation, and require + validate the token in `/auth/status`.

## 4. Checked and ruled out

- MariaDB statements in `app/clients/blackboard.py` all use `%s` placeholders; `set_state`
  allowlists states. No injection.
- `/ui/blackboard/trace/{row_id}` correctly checks the session token (401 otherwise); the
  iframe is sandboxed without `allow-same-origin`.
- NiceGUI markdown rendering of agent `finding` is sanitized by default; trace viewer
  sandboxed.
- No secrets committed: `.env` gitignored, `.env.example` has no credentials; only the two
  intentional placeholder defaults (covered by F2).
- GitHub Actions uses `GITHUB_TOKEN`/`secrets.*` only; no hardcoded creds; no
  `pull_request_target`.
- `app/clients/ckan.py` wraps `json.loads` of CKAN extras in `try/except`.

## 5. Unknowns / needs runtime verification

- Whether production sets `AUTH_MASTER_SECRET`/`STORAGE_SECRET` (determines F2
  exploitability; manifests live in the separate `episerve-k8s` repo).
- Whether the UI is reachable from untrusted networks or only inside the cluster boundary
  — RESOLVED for this tester: `https://api.episerve.zib.de/ui/datasets` returned HTTP 200
  anonymously (internet-reachable from the tester's network).
- Exact DuckDB in the deployed image vs. local 1.5.5 — RESOLVED: live `SELECT version()`
  returned `v1.5.6`. Whether a native-code extension is loadable (would raise F1 from file
  read/write to confirmed RCE) remains unknown.
- Whether anyone beyond trusted operators can write CKAN/lakeFS/DOIP metadata (F3).
- Dependency CVEs not assessed (no `pip-audit`/`osv-scanner` run); `requirements.txt` pins
  only minimum versions.

## 6. Notes on earlier write attempts

The requested combined document did not exist prior to this file. Investigation found:

- `pi` session log shows exactly four `write` calls, all for the tooling artifacts
  (`SKILL.md`, `report-format.md`, `run-scan.sh`, `audit.md`). No failed writes logged.
- No `setup + analysis + findings` `.md` file was present in `/home/tim`.
- The directory is writable (probe touch succeeded; 81 GB free) — writes are not blocked.
- The only findings file was the complete raw report at
  `sec-reports/episerve_api_server-20261013-115425/security-audit.md`.

Conclusion: the file was never created; there is no write-permission or disk fault. This
document is the requested combined file.

## 7. Live verification on production (2026-10-13)

**Target:** `https://api.episerve.zib.de` — the deployed EPISERVE API server.
**Result:** F1 CONFIRMED in production. Any internet client can run arbitrary DuckDB SQL
server-side, anonymously, and read server files.

### What was confirmed

| Claim | Method | Observed result |
|---|---|---|
| `/ui/datasets` is anonymously reachable | `GET` with no credentials | HTTP 200; also `/ui` 200, `/health` 200 |
| SQL input is exposed without auth | headless browser: load page, tick one dataset row | SQL box became visible; default preview returned `1-10 of 500` real records |
| Arbitrary SQL executes server-side | submit `SELECT version()` | `v1.5.6` (deployed DuckDB) |
| Filesystem access is enabled | submit `SELECT current_setting('enable_external_access')` | `True` |
| Arbitrary server file read | submit `SELECT * FROM read_text('/etc/hostname')` | hostname `episerve-api-server-84bcb74ddf-7686p` (a Kubernetes pod name) |

### How it was confirmed

1. **Reconnaissance (curl):** probed `/`, `/ui/datasets`, `/ui`, `/health`, `/docs`,
   `/openapi.json` — all returned 200 with no auth challenge; `/ui/datasets` returned 52 KB
   of HTML containing a `SQL query` control. NiceGUI 3.18.0 websocket endpoint
   `/_nicegui_ws/socket.io/` returned 200.
2. **UI driving (Playwright + system Chromium, headless):** loaded
   `/ui/datasets`, clicked the first dataset row's selection checkbox (triggering
   `on_selection`, which sets `current_url` and reveals the SQL row), filled the `SQL query`
   input, clicked **Run**, and read the returned table from the page body.
3. **Payloads were read-only:** only `SELECT ...` statements. No writes, no
   `COPY ... TO`, no extension installation, no code execution.

### Why the result is server-side (not a browser artifact)

`_duckdb_query` runs in the app process via `run.io_bound(...)`. The value read from
`/etc/hostname` is `episerve-api-server-84bcb74ddf-7686p` — a Kubernetes pod name — whereas
the tester's own machine reports `/etc/hostname` as `Surface9`. The read therefore occurred
inside the EPISERVE pod.

### Scope and safety

- Non-destructive: no files were written, no secrets were read or exfiltrated, no RCE was
  attempted. Read-only SQL only.
- Evidence screenshot: `/tmp/f1_poc.png` (full page, showing the pod hostname in the result
  table).
- Deployed DuckDB is **v1.5.6** (report previously cited repo-local 1.5.5 / tester 1.5.3).

## 8. Independent code-only review (2026-10-13)

- Revision: `47a77eab6b7f7d792dbc0a81f56641b62c48708a`.
- Working tree at review start: tracked files unchanged; this report was untracked.
- Reviewer: Codex.
- Scope: repository source, configuration, Dockerfile, workflow, and relevant locally
  installed NiceGUI source. Installed dependency source is supporting evidence, not
  proof of the versions or configuration deployed in production.
- Scanners run: none; `semgrep`, `gitleaks`, `trivy`, `osv-scanner`, and `pip-audit`
  were not on PATH. No scanner installation was attempted.
- No application startup, tests, SQL execution, browser automation, backend calls,
  production requests, or secret inspection were performed. Official DuckDB
  documentation was consulted to check engine capabilities, defaults, and remediation.

**Verdict:** The four core weaknesses are supported by code, but the document cannot
be confirmed verbatim. F1 is confirmed by a checkable source trace; production
exploitation and RCE are not established by this review. F2 is conditional on secret
configuration, and its cookie-forgery claim overstates what the reviewed NiceGUI
implementation permits. F3's preview page is also unauthenticated. F4 uses local-date
rotation, despite reporting UTC expiry.

### F1 — Anonymous unrestricted DuckDB SQL — P1 — CONFIRMED

- **Confidence / prerequisites:** High for the source trace. The attacker must reach
  the UI; CKAN must return a selectable dataset with a usable Parquet component;
  httpfs installation/loading and the initial `CREATE VIEW` must succeed.
- **Location / trace:** `app/main.py:8-17,251-258` installs no authentication gate for
  the UI. `app/ui/pages.py:258-262` renders the datasets page without `_require_login`.
  Selection at `:435-464` sets the component URL and exposes the SQL control after
  a successful preview. The input at `:522` reaches `run_sql` at `:505-512`, then
  `_duckdb_query` at `:338-344`, which calls `conn.execute(sql).df()` verbatim.
  `:346-355` sends result rows to the browser through NiceGUI.
- **Invariant / boundary / impact:** An anonymous browser controls SQL executing in
  the server process, without query restrictions or external-access hardening.
  File read/write and outbound requests are supported engine capabilities under
  the default configuration, subject to process permissions and network controls.
  Root is the image default because `Dockerfile:1-13` sets no `USER`; Kubernetes
  could override that. Secret exposure depends on which files are actually mounted.
  RCE through extensions remains a hypothesis, not a confirmed consequence.
- **Disproof check:** No page login gate, global auth dependency, SQL allowlist, or
  hardened connection configuration appears in this call path. The preliminary
  httpfs/view operations are real reachability conditions, not security controls.
- **Remediation:** Remove unrestricted SQL from the public page. If custom queries
  are required, use a constrained query interface over server-loaded data in an
  isolated worker with no secrets, restricted egress, and resource limits. A login
  check and non-root execution are useful additional controls.

DuckDB documents filesystem primitives and recommends operating-system isolation
for untrusted SQL in its [security guidance](https://duckdb.org/docs/current/operations_manual/securing_duckdb/overview).
Its [configuration reference](https://duckdb.org/docs/current/configuration/overview)
lists external access as enabled by default. Disabling external access also blocks
the current external `read_parquet` path, so the original remediation is not a
drop-in fix: load approved data separately before exposing a restricted query engine.
Allowing only `SELECT` is insufficient when file/network table functions are available.

### F2 — Known fallback authentication and signing secrets — P1 — LIKELY

- **Confidence / prerequisites:** High for the fallback code; production use is
  unknown. An attacker must reach the service while the relevant secret uses its
  published fallback. The two environment settings are independent.
- **Location / trace:** `app/config.py:50` supplies the authentication fallback;
  `app/auth.py:13-23` derives and accepts the daily HMAC token. Under that fallback,
  an attacker can derive the accepted API token without login credentials.
  `app/routers/model_runs.py:11-20` accepts it and forwards caller-supplied image,
  tag, inputs, and transformation SQL to Prefect (`app/clients/prefect.py:22-42`).
- **Invariant / boundary / impact:** Public information becomes an authenticated API
  credential, enabling unauthorized flow-run submission. Whether the chosen image
  actually executes, and with what cluster privileges, depends on the external
  Prefect flow and deployment; those are not in this repository.
- **Correction:** `app/main.py:257` also uses a known signing fallback, but NiceGUI's
  reviewed local implementation stores `storage.user` on the server, keyed by the
  cookie's session ID (`.venv/lib/python3.12/site-packages/nicegui/storage.py:31-44,134-154`).
  Signing a cookie with an arbitrary `token` field does not populate that server-side
  token. Impersonating an authenticated session through this mechanism additionally
  requires its session ID and usable server-side state. The report's independent
  cookie-forgery-to-full-UI-access path is not established. Likewise, the login UI
  requires username/password (`app/ui/pages.py:235-241`); it accepts no bearer-token
  login. Knowing the API token alone is not the demonstrated UI login path.
- **Disproof check:** No startup rejection of fallback secrets appears in the repo;
  `.env.example` does mention `AUTH_MASTER_SECRET` in a comment, but assigns neither
  it nor `STORAGE_SECRET`. Deployment overrides are outside this review.
- **Remediation:** Require independent strong secrets at startup and reject missing,
  empty, or placeholder values. Document both environment variables. Replace the
  shared authentication credential with individually issued, revocable credentials.

Additional configuration concern: `AUTH_PASSWORD` defaults to an empty string
(`app/config.py:52`), and both API and UI login compare against it without rejecting
an empty configured password. Startup validation should reject this too.

### F3 — Catalog-controlled SQL and outbound preview requests — P2 — CONFIRMED

- **Confidence / prerequisites:** High for unsafe data flows; an attacker needs control
  over consumed catalog metadata for the persisted attack described in the report.
  Actual metadata write permissions and internal network reachability are unknown.
- **Location / trace:** CKAN resource URLs reach `read_parquet` by string interpolation
  via `app/clients/ckan.py:55-60` and `app/ui/pages.py:453-460,341`.
  On model-run previews, provenance `@id` and `schema:query` reach `_fetch_preview`
  through `:958-968` and `:1152-1160`; SQL and URLs are interpolated at `:867-877`.
  Arbitrary query expressions can therefore invoke file/network functions even
  without a successful stacked-statement injection.
- **Invariant / boundary / impact:** Catalog data crosses into privileged SQL and
  network operations. Attacker-controlled provenance URLs can direct outbound
  requests; quote-bearing URLs can alter SQL syntax. This supports file disclosure
  or internal requests under the engine/process permissions, with results displayed
  to the previewing client. Specific targets and payload success were not tested.
- **Corrections / disproof check:** `/ui/model-runs` has no login gate
  (`app/ui/pages.py:678-681`), so anonymous users can trigger previews of poisoned
  metadata. The report confuses this with the metadata-writer prerequisite.
  `_fetch_metadata` does call `requests.get` at `:852`, but the normal CKAN producer
  constructs `doip_url` using the configured base (`app/clients/ckan.py:18-19,109`),
  rather than accepting an arbitrary catalog host. The provenance URL path is the
  clearer arbitrary-destination evidence. Model-run previews also require a
  predictions component (`app/ui/pages.py:922-927`), and provenance filenames must
  pass the `.parquet` check at `:960-962`.
- **Remediation:** Bind URL values where supported rather than interpolating them;
  eliminate untrusted SQL or constrain it in the same isolated engine as F1.
  Validate destination schemes/hosts and redirects, and enforce egress restrictions.

### F4 — Shared daily bearer credential and unconditional status — P2 — CONFIRMED

- **Confidence / prerequisites:** High. An attacker must obtain the shared credential;
  seeing the public page or token-menu label alone does not disclose it. The dialog
  reads the current session's token (`app/ui/pages.py:52-54`), not `_daily_token()`.
- **Location / trace:** `app/auth.py:13-23` derives one accepted token from secret plus
  date, with no principal or revocation lookup. `app/routers/auth.py:38-43` returns
  this shared token after checking the single configured username/password pair.
  `:46-48` returns `valid=True` without receiving or verifying credentials.
- **Invariant / boundary / impact:** A leaked shared bearer token grants model-run
  submission to whoever possesses it until it changes. Logging out only clears
  local session storage (`app/ui/pages.py:62-65`); it does not revoke that bearer
  credential. The status endpoint reports success even for unauthenticated callers,
  though it does not itself bypass authorization on `POST /model-runs`.
- **Correction / disproof check:** `date.today()` uses the process's local calendar
  date, while `_next_midnight()` returns UTC midnight (`app/routers/auth.py:29-34`).
  UTC rotation is true only if the process's local timezone is UTC. This repository
  does not establish the runtime timezone or individual credential revocation.
- **Remediation:** Use individually issued credentials with explicit expiry and
  revocation. Validate a supplied credential in `/auth/status`, and use a consistent
  timezone for any remaining time calculations. Shared-token design alone is not
  an unauthenticated bypass when the master secret is strong.

### Limits of the original "checked and ruled out" statements

- Blackboard SQL uses bound values and an allowlisted state; that narrow conclusion
  is supported by `app/clients/blackboard.py:50,89-92,120-126,187-192,204-206`.
- The trace route checks the session token and the embedded iframe is sandboxed
  (`app/ui/pages.py:1417,1859-1864`). However, direct authenticated navigation serves
  raw trace HTML without a response-level sandbox in this code. The iframe alone
  does not establish safety for that navigation; malicious-trace provenance and
  deployment response headers were not assessed.
- The installed NiceGUI markdown implementation sanitizes by default; application
  rendering uses that default. This does not prove behavior for every deployed
  dependency version, since requirements are unpinned.
- CKAN JSON handling is not universally wrapped: `app/clients/ckan.py:35` calls
  `json.loads(raw_params)` without `try/except`; only the run-related fields at
  `:88-96` have that protection.
- `.env` is ignored and the workflow contains secret references rather than literal
  credentials. "No secrets committed" is broader than the evidence: neither a
  secrets scanner nor a full-history secrets review was performed here.
- Earlier live observations, tooling setup, external report paths, exact production
  versions, network exposure, mounted secrets, and deployment secret configuration
  remain unverified by this code-only review. Dependency CVEs were not assessed.

## 9. F1 remediation in the working tree (2026-10-13)

The `/ui/datasets` arbitrary SQL input and execution callback have been removed.
`app/dataset_preview.py` builds queries from schema-validated columns, allowlisted
operators/directions, and bound filter/pagination values. Dataset selections are
resolved against server-held catalog records; browser-provided component URLs are
ignored. Component loading uses HTTP outside DuckDB, permits only the configured
DOIP origin and retrieve path, and rejects redirects and unsafe path normalization.
The query connection reads a registered Arrow table with external access and
automatic extension installation/loading disabled and configuration locked.

`app/ui/pages.py` exposes column, filter, sorting, page-size, and navigation controls.
Download and decoded-sample limits are 64 MiB each; queries cover at most the first
50,000 source rows, with sampling explicitly identified in the page. This changes
the former remote range-read behavior to a bounded full-component download.

Regression tests exercise SQL-looking values and identifiers, forged controls and
selections, URL restrictions, redirects, byte/sample limits, typed queries, and
the anonymous page's load/filter/pagination callbacks. All network-dependent data
in these tests is mocked; no production test or deployment was performed.

This records the F1 fix in source, not production remediation. F2 and F4 remain
unchanged. F3's separate model-run preview SQL/URL paths remain unchanged and must
be addressed independently; the dataset URL interpolation portion is removed by
this change.

## 10. F3 remediation in the working tree (2026-10-13)

`app/model_run_preview.py` resolves selections against server-held catalog run
identifiers and derives metadata URLs from the configured DOIP base. Browser row
URLs and metadata's own run identifier cannot redirect preview requests. Saved
component paths must stay in the input/output namespace; original input URLs must
pass the same origin/path validation used by dataset previews.

Parquet previews now use bounded downloads and generated queries over registered
Arrow data in a connection with external access disabled. CSV/TSV downloads are
also validated, bounded, and redirect-free. Metadata is limited to 1 MiB, runs to
20 preview components, and displayed rows to 1–500 per component. All limits and
validation apply to both initial loading and changes to the row-limit control.

The page never executes `schema:query`. Recorded SQL is displayed as provenance
only. Saved inputs take priority; fallback original inputs are clearly identified
as data whose transformations have not been reapplied. The row-limit callback uses
the same validated component list, preserving that preference. Stale callbacks
cannot replace another selected run's preview.

Regression tests cover malicious metadata SQL and paths, forged browser selections
and metadata identifiers, original-input destination validation, redirects and
response-size limits, text-prefix handling, saved-input preference, and the actual
initial/reload UI callbacks. Backends are mocked; no live testing or deployment was
performed. This records source remediation of F3's preview paths. F2 and F4 remain
unchanged, and the earlier findings describe the previously reviewed revision.

