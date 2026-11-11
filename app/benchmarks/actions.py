"""Design, check, finalize and retire benchmarks; build run specs.

A benchmark is edited as a draft and frozen by ``finalize``, which pins the current lakeFS
head commit of ``data-processed``: the reference data is read at that commit and the run
spec makes model runs read their input at the same commit.
"""

import json
from io import BytesIO
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone

import pandas as pd

from app.benchmarks import descriptor, fdo, materialize, preview, recipe, store
from app.benchmarks.definition import (
    Benchmark, DefinitionError, derive_horizon, input_uri, materialized_from_row,
    normalize_covariates, normalize_filters, target_preview_from_row, validate,
)
from app.benchmarks.errors import Conflict, Forbidden, NotDraft, NotFound  # noqa: F401  (re-exported)
from app.clients import ckan as ckan_client
from app.clients import lakefs as lakefs_client
from lakefs.exceptions import NotFoundException

from app.config import settings
from app.dataset_preview import MAX_DOWNLOAD_BYTES
from app.leaderboard import loaders
from app.leaderboard.loaders import LoadError
from app.users import User

_EDITABLE = {
    "name", "description", "dataset_qid", "target_column", "date_column", "test_start", "test_end",
    "filters", "covariates", "history_start", "history_end", "input_filename",
}


@dataclass
class CheckReport:
    """What a check of a draft found (problems block finalization, warnings do not)."""

    ok: bool
    problems: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    data_commit: str | None = None
    component_name: str | None = None
    truth_points: int | None = None
    history_end: date | None = None
    last_history_date: date | None = None
    prediction_length: int | None = None
    frequency_days: int | None = None
    columns: list[str] | None = None            # the columns of the generated input file (benchmarks with covariates)
    missing: dict | None = None                 # share of empty values per covariate column
    input_rows: int | None = None
    built: object = field(default=None, repr=False)      # the generated tables, kept for finalize
    sources: list = field(default_factory=list)           # the datasets (QID, file) the tables were built from


def _dataset(qid: str) -> dict:
    dataset = next((d for d in ckan_client.list_processed_datasets() if d["qid"] == qid), None)
    if dataset is None:
        raise NotFound(f"Dataset {qid!r} not found")
    return dataset


def _row(benchmark_id: int) -> dict:
    row = store.get(benchmark_id)
    if row is None:
        raise NotFound(f"Benchmark {benchmark_id} not found")
    return row


def _draft_row(benchmark_id: int) -> dict:
    row = _row(benchmark_id)
    if store.state(row) != "draft":
        raise NotDraft(f"Benchmark {benchmark_id} is final or retired and cannot be changed.")
    return row


def create_draft(
    name: str,
    description: str | None,
    dataset_qid: str,
    target_column: str,
    date_column: str,
    test_start: date,
    test_end: date,
    filters=(),
    history_start: date | None = None,
    history_end: date | None = None,
    input_filename: str = "input.parquet",
    created_by: int | None = None,
    covariates=(),
) -> int:
    """Validate a definition and store it as a draft made by the user with id ``created_by``.

    Raises:
        DefinitionError: Invalid definition.
        NotFound: Unknown dataset.
    """
    benchmark = Benchmark(
        dataset_qid=dataset_qid, target_column=(target_column or "").strip(), date_column=(date_column or "").strip(),
        test_start=test_start, test_end=test_end, filters=normalize_filters(filters),
        history_start=history_start, history_end=history_end, input_filename=input_filename,
        covariates=normalize_covariates(covariates),
    )
    validate(benchmark, name)
    _dataset(dataset_qid)
    for covariate in benchmark.covariates:
        _dataset(covariate.dataset_qid)
    return store.create_draft(name.strip(), description, benchmark, created_by)


def update_draft(benchmark_id: int, **fields) -> None:
    """Change fields of a draft (only the given ones; ``None`` clears an optional field).

    Raises:
        NotFound: Unknown benchmark or dataset.
        NotDraft: The benchmark is final or retired.
        DefinitionError: The merged definition is invalid.
    """
    unknown = set(fields) - _EDITABLE
    if unknown:
        raise DefinitionError(f"Unknown field(s): {', '.join(sorted(unknown))}.")
    row = _draft_row(benchmark_id)
    current = store.benchmark_from_row(row)
    name = fields.get("name", row["name"])
    description = fields.get("description", row["description"])
    changes = {k: v for k, v in fields.items() if k not in ("name", "description")}
    for key in ("target_column", "date_column"):
        if isinstance(changes.get(key), str):
            changes[key] = changes[key].strip()
    if "filters" in changes:       # the whole list of filters at once (null or empty removes them)
        changes["filters"] = normalize_filters(changes["filters"])
    if "covariates" in changes:    # the whole list of covariates at once (null or empty removes them)
        changes["covariates"] = normalize_covariates(changes["covariates"])
    merged = replace(current, **changes)
    validate(merged, name)
    if "dataset_qid" in changes:
        _dataset(merged.dataset_qid)
    for covariate in merged.covariates:
        _dataset(covariate.dataset_qid)
    if not store.update_draft(benchmark_id, name.strip(), description, merged):
        raise NotFound(f"Benchmark {benchmark_id} not found")


def _evaluate(benchmark: Benchmark, dataset: dict, commit: str, component_name: str | None = None) -> CheckReport:
    """Read the data at ``commit``, build the model input and the reference, and report problems, warnings and the
    derived values. ``component_name`` is the pinned file of an already finalized benchmark."""
    report = CheckReport(ok=False, data_commit=commit)
    if dataset.get("access") == "restricted":
        report.problems.append(f"Dataset {dataset['qid']} is restricted; benchmarks are always open.")
        return report
    components = dataset.get("components") or []
    if not components:
        report.problems.append("The dataset has no downloadable component.")
        return report
    report.component_name = component_name or components[0]["name"]
    try:
        data = lakefs_client.read_processed_component(commit, dataset["qid"], report.component_name, MAX_DOWNLOAD_BYTES)
    except NotFoundException:
        report.problems.append("The dataset file does not exist at the pinned commit.")
        return report
    except ValueError as exc:
        report.problems.append(str(exc))
        return report
    try:
        history_end = benchmark.history_end
        if history_end is None:      # the last observation before the test window
            before = benchmark.test_start - timedelta(days=1)
            series = loaders.read_series(
                data, benchmark, date(1900, 1, 1), before, require_unique=False,
                empty_message=f"the dataset has no observations on or before {before.isoformat()}")
            history_end = series["date"].max().date()
        elif history_end >= benchmark.test_start:
            raise LoadError("The history must end before the test start.")
        report.history_end = history_end
        resolved = replace(benchmark, history_end=history_end)
        sources = [{"role": "target", "qid": dataset["qid"], "component": report.component_name}]
        built = materialize.build(resolved, history_end, data, _covariate_data(benchmark, commit, sources))
        report.sources = sources
    except LoadError as exc:
        report.problems.append(str(exc))
        return report
    report.truth_points = len(built.reference)
    report.last_history_date = built.input[benchmark.date_column].max()
    try:
        horizon = derive_horizon(list(built.reference[benchmark.date_column]), report.last_history_date)
    except DefinitionError as exc:
        report.problems.append(str(exc))
        return report
    report.prediction_length, report.frequency_days = horizon.prediction_length, horizon.frequency_days
    if not horizon.regular:
        report.warnings.append(
            "The observed dates are not evenly spaced; one forecast grid may not hit every observed date."
        )
    report.warnings.extend(built.warnings)
    report.columns, report.missing, report.input_rows, report.built = (
        built.columns, built.missing, len(built.input), built)
    report.ok = True
    return report


def _covariate_data(benchmark: Benchmark, commit: str, sources: list | None = None) -> dict[str, bytes]:
    """The Parquet bytes of every covariate's dataset at ``commit``; raises ``LoadError`` with the reason.

    ``sources`` (when given) collects ``{"role", "qid", "component"}`` for every covariate read."""
    out = {}
    for covariate in benchmark.covariates:
        try:
            dataset = _dataset(covariate.dataset_qid)
        except NotFound as exc:
            raise LoadError(f"covariate {covariate.name!r}: {exc}") from exc
        if dataset.get("access") == "restricted":
            raise LoadError(f"covariate {covariate.name!r}: dataset {dataset['qid']} is restricted; "
                            "benchmarks are always open")
        components = dataset.get("components") or []
        if not components:
            raise LoadError(f"covariate {covariate.name!r}: the dataset has no downloadable component")
        try:
            out[covariate.name] = lakefs_client.read_processed_component(
                commit, covariate.dataset_qid, components[0]["name"], MAX_DOWNLOAD_BYTES)
            if sources is not None:
                sources.append({"role": covariate.name, "qid": covariate.dataset_qid,
                                "component": components[0]["name"]})
        except NotFoundException as exc:
            raise LoadError(f"covariate {covariate.name!r}: the dataset file does not exist at the pinned commit") from exc
        except ValueError as exc:
            raise LoadError(f"covariate {covariate.name!r}: {exc}") from exc
    return out


def _manifest(benchmark_id: int, name: str, benchmark: Benchmark, built: "materialize.Built",
              sources: list | None, view: dict) -> dict:
    """Store data, a portable definition, and FDO metadata in one lakeFS commit.

    Args:
        benchmark_id: Benchmark receiving the generated object.
        name: Display name recorded in the object's metadata.
        benchmark: Definition pinned to its source data commit.
        built: Exact input and reference tables produced during finalization.
        sources: Source object identifiers and component names.
        view: Resolved benchmark row used to freeze the generation recipe.

    Returns:
        Manifest containing file checksums, provenance, recipe, and descriptor pointer.
    """
    input_bytes, input_sha = materialize.to_parquet(built.input)
    reference_bytes, reference_sha = materialize.to_parquet(built.reference)
    qid = fdo.mint_qid()
    source_qids = sorted({benchmark.dataset_qid, *(c.dataset_qid for c in benchmark.covariates)})
    parts = {
        "input": ("input.parquet", input_bytes, input_sha, built.input),
        "reference": ("reference.parquet", reference_bytes, reference_sha, built.reference),
    }
    manifest = {"version": 3, "repo": settings.lakefs_benchmarks_repo, "branch": settings.lakefs_branch,
                "qid": qid, "sources": sources or []}
    files = {}
    for role, (component, data, sha, frame) in parts.items():
        files[fdo.component_path(qid, component)] = data
        manifest[role] = {"qid": qid, "component": component, "sha256": sha, "rows": len(frame),
                          "columns": list(frame.columns)}
    manifest["input"].update(first_date=str(built.input[benchmark.date_column].min()),
                             last_date=str(built.input[benchmark.date_column].max()))
    manifest["recipe"] = recipe.describe(view, manifest)
    definition_bytes = descriptor.build(view, manifest)
    manifest["definition"] = descriptor.file_metadata(qid, definition_bytes)
    files[fdo.component_path(qid, descriptor.COMPONENT)] = definition_bytes
    files[fdo.fdo_path(qid)] = fdo.build_fdo(
        qid=qid, benchmark_id=benchmark_id, benchmark_name=name,
        files={role: {k: v for k, v in manifest[role].items() if k != "qid"}
               for role in (*parts, "definition")},
        benchmark=benchmark, source_qids=source_qids, sources=sources,
        description=f"{name}. Generated model input (history window, target series and covariates) and "
                    "reference data (the test window the forecasts are scored against).")
    branch, commit = lakefs_client.write_benchmark_objects(
        files, f"benchmark {benchmark_id}: generated files and frozen definition")
    manifest.update(branch=branch, commit=commit)
    return manifest


def export_definition(benchmark_id: int) -> dict:
    """Export an existing finalized definition without replacing its data objects.

    Args:
        benchmark_id: Final benchmark whose descriptor has not yet been exported.

    Returns:
        Descriptor pointer, including the new metadata commit and checksum.

    Raises:
        NotFound: The benchmark is not final or lacks a generated FDO object.
        Conflict: Its manifest changed during the export.
    """
    row = _row(benchmark_id)
    manifest = materialized_from_row(row)
    if store.state(row) != "final" or not manifest or not manifest.get("qid"):
        raise NotFound(f"Benchmark {benchmark_id} has no finalized generated object")
    if manifest.get("definition"):
        return manifest["definition"]
    frozen = {**manifest, "recipe": recipe.display(row, manifest)}
    data = descriptor.build(row, frozen)
    pointer = descriptor.file_metadata(manifest["qid"], data)
    document = lakefs_client.read_benchmark_fdo(manifest["commit"], manifest["qid"], 4 * 1024 * 1024)
    recorded = document["profile"]["episerveBenchmark"]
    if document["@id"] != manifest["qid"] or recorded["id"] != benchmark_id \
            or recorded["data_commit"] != row["data_commit"]:
        raise Conflict("The frozen FDO does not match the benchmark identity or source commit")
    for kind in ("input", "reference"):
        if any(recorded["files"][kind].get(key) != manifest[kind].get(key)
               for key in ("component", "sha256", "rows", "columns")):
            raise Conflict("The frozen FDO and database file metadata differ")
    document["kernel"]["fdo:hasComponent"].append({
        "@id": f"#{descriptor.COMPONENT}", "componentId": descriptor.COMPONENT, "mediaType": "application/json"})
    document["profile"]["episerveBenchmark"]["files"]["definition"] = {
        key: value for key, value in pointer.items() if key != "qid"}
    document["kernel"]["modified"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    _, commit = lakefs_client.write_benchmark_objects({
        fdo.component_path(manifest["qid"], descriptor.COMPONENT): data,
        fdo.fdo_path(manifest["qid"]): json.dumps(document, ensure_ascii=False, indent=2).encode(),
    }, f"benchmark {benchmark_id}: export frozen definition")
    pointer["commit"] = commit
    if not store.set_definition(benchmark_id, pointer, expected_commit=manifest["commit"]):
        current = materialized_from_row(_row(benchmark_id)) or {}
        if current.get("commit") == manifest["commit"] and current.get("definition"):
            return current["definition"]
        raise Conflict("The benchmark manifest changed while its definition was exported")
    return pointer


def store_target_preview(benchmark_id: int) -> dict:
    """Fill the dedicated preview column using an existing benchmark's frozen files.

    Args:
        benchmark_id: Final benchmark whose preview column should be populated.

    Returns:
        The stored target preview, including an existing image when present.

    Raises:
        NotFound: The benchmark is not final or has no generated files.
        Conflict: The manifest changed while its preview was being generated.
        LoadError: Generated file contents do not match their recorded hashes.
    """
    import hashlib

    row = _row(benchmark_id)
    manifest = materialized_from_row(row)
    if store.state(row) != "final" or not manifest:
        raise NotFound(f"Benchmark {benchmark_id} has no finalized generated files")
    existing = target_preview_from_row(row)
    if existing:
        return existing
    frames = {}
    for kind in ("input", "reference"):
        info = manifest[kind]
        if not info.get("qid"):
            raise NotFound(f"Benchmark {benchmark_id} needs its generated object migrated first")
        data = lakefs_client.read_benchmark_component(
            manifest["commit"], info["qid"], info["component"], MAX_DOWNLOAD_BYTES)
        if hashlib.sha256(data).hexdigest() != info["sha256"]:
            raise LoadError(f"The {kind} file does not match its frozen checksum")
        frames[kind] = pd.read_parquet(BytesIO(data))
    result = preview.build(store.benchmark_from_row(row), materialize.Built(**frames))
    if not store.set_target_preview(benchmark_id, result, expected_commit=manifest["commit"]):
        current_row = _row(benchmark_id)
        current = materialized_from_row(current_row) or {}
        existing = target_preview_from_row(current_row)
        if current.get("commit") == manifest["commit"] and existing:
            return existing
        raise Conflict("The benchmark manifest changed while its preview was generated")
    return result


def check(benchmark_id: int) -> CheckReport:
    """Check a draft against the current data (lakeFS head); nothing is stored.

    Raises:
        NotFound / NotDraft: Unknown benchmark or not a draft.
    """
    row = _draft_row(benchmark_id)
    benchmark = store.benchmark_from_row(row)
    return _evaluate(benchmark, _dataset(benchmark.dataset_qid), lakefs_client.processed_head_commit())


def may_finalize(row: dict, user: User) -> bool:
    """Only the creator finalizes a draft; one without a recorded creator (made before creators were kept)
    can be finalized by an admin."""
    creator = row.get("created_by")
    return user.can("admin") if creator is None else user.id == creator


def finalize(benchmark_id: int, expected_commit: str | None = None, *, by: User) -> None:
    """Freeze a draft and pin the lakeFS head commit; only its creator (``by``) may do this.

    With ``expected_commit`` (the commit of the last check) the finalization is refused when
    the head moved since.

    Args:
        benchmark_id: Draft whose data and preview should be frozen.
        expected_commit: Optional source commit observed by the previous check.
        by: Authenticated user requesting finalization.

    Raises:
        Forbidden: ``by`` is not the creator of the draft.
        Conflict: ``expected_commit`` differs from the current head commit.
        NotFound / NotDraft: Unknown benchmark or not a draft.
        LoadError: The check found problems (all of them in the message).
    """
    row = _draft_row(benchmark_id)
    if not may_finalize(row, by):
        raise Forbidden("Only the creator of this draft can finalize it." if row.get("created_by") is not None
                        else "This draft has no recorded creator; only an admin can finalize it.")
    benchmark = store.benchmark_from_row(row)
    dataset = _dataset(benchmark.dataset_qid)
    commit = lakefs_client.processed_head_commit()
    if expected_commit is not None and expected_commit.lower() != commit.lower():
        raise Conflict(
            f"The data changed since the check (checked {expected_commit[:12]}, now {commit[:12]}); check again."
        )
    report = _evaluate(benchmark, dataset, commit)
    if not report.ok:
        raise LoadError("; ".join(report.problems))
    again = _row(benchmark_id)
    if (again["name"], store.benchmark_from_row(again)) != (row["name"], benchmark):
        raise Conflict("The draft changed while it was being finalized; check it again.")
    finalized_at = datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)
    view = {**row, "history_end": report.history_end, "data_commit": commit, "component_name": report.component_name,
            "truth_points": report.truth_points, "prediction_length": report.prediction_length,
            "finalized_at": finalized_at}
    manifest = _manifest(benchmark_id, row["name"], replace(benchmark, data_commit=commit), report.built,
                         report.sources, view)
    target_preview = preview.build(benchmark, report.built)
    done = store.finalize(
        benchmark_id, data_commit=commit, component_name=report.component_name, truth_points=report.truth_points,
        prediction_length=report.prediction_length, history_end=report.history_end, materialized=manifest,
        target_preview=target_preview, finalized_at=finalized_at,
    )
    if not done:
        raise NotFound(f"Benchmark {benchmark_id} not found")


def materialize_final(benchmark_id: int) -> dict:
    """Generate and store the files of a final benchmark that has none or only an older layout (files below the benchmark id, or one object per file).

    The data is read at the benchmark's own pinned commit, so the result is what finalizing would have produced.

    Raises:
        NotFound / Conflict: Unknown, not final, or already has generated files.
        LoadError: The data at the pinned commit cannot be turned into the files.
    """
    row = _row(benchmark_id)
    if store.state(row) != "final":
        raise NotFound(f"Benchmark {benchmark_id} is not final")
    if (materialized_from_row(row) or {}).get("version", 1) >= 3:
        raise Conflict(f"Benchmark {benchmark_id} already has its generated object.")
    benchmark = store.benchmark_from_row(row)
    report = _evaluate(benchmark, _dataset(benchmark.dataset_qid), benchmark.data_commit, row["component_name"])
    if not report.ok:
        raise LoadError("; ".join(report.problems))
    manifest = _manifest(benchmark_id, row["name"], benchmark, report.built, report.sources, row)
    store.set_materialized(benchmark_id, manifest)
    return manifest


def store_recipe(benchmark_id: int) -> dict:
    """Keep (or replace) the description of how a final benchmark's files were generated with its manifest, in the
    current format; the sources default to the benchmark's target dataset.

    Raises:
        NotFound: Unknown, not final or without generated files.
    """
    row = _row(benchmark_id)
    manifest = materialized_from_row(row)
    if store.state(row) != "final" or not manifest:
        raise NotFound(f"Benchmark {benchmark_id} has no generated files")
    if not manifest.get("sources"):
        manifest["sources"] = [{"role": "target", "qid": row["dataset_qid"], "component": row["component_name"]}]
    manifest["recipe"] = recipe.describe(row, manifest)
    store.set_materialized(benchmark_id, manifest)
    return manifest


def set_description(benchmark_id: int, description: str | None) -> None:
    """Change the description, also of a final benchmark (empty clears it).

    Raises:
        NotFound: Unknown benchmark.
    """
    _row(benchmark_id)
    store.set_description(benchmark_id, (description or "").strip() or None)


def retire(benchmark_id: int) -> None:
    """Hide a benchmark from the lists.

    Raises:
        NotFound: Unknown or already retired.
    """
    _row(benchmark_id)
    if not store.retire(benchmark_id):
        raise NotFound(f"Benchmark {benchmark_id} is already retired")


def get(benchmark_id: int, *, include_draft: bool = False) -> dict:
    """One benchmark row; never-finalized ones (drafts, retired drafts) only with ``include_draft``.

    Raises:
        NotFound: Unknown, or never finalized without ``include_draft``.
    """
    row = _row(benchmark_id)
    if not store.is_public(row) and not include_draft:
        raise NotFound(f"Benchmark {benchmark_id} not found")
    return row


def list_final() -> list[dict]:
    """Final, not retired benchmarks (public)."""
    return store.list_final()


def list_drafts() -> list[dict]:
    """Draft benchmarks (for logged-in users)."""
    return store.list_drafts()


def run_spec(benchmark_id: int) -> dict:
    """Everything the run workflow prefills for a final benchmark (no external call).

    Raises:
        NotFound: Unknown, draft or retired.
    """
    row = _row(benchmark_id)
    if store.state(row) != "final":
        raise NotFound(f"Benchmark {benchmark_id} is not final")
    benchmark = store.benchmark_from_row(row)
    manifest = materialized_from_row(row)
    if not manifest:
        raise NotFound(f"Benchmark {benchmark_id} has no generated input file yet")
    return {
        "benchmark_id": row["id"],
        "dataset_qid": benchmark.dataset_qid,
        "data_commit": benchmark.data_commit,
        "input_data_files": [[input_uri(manifest), benchmark.input_filename]],
        "data_transformation_sql": [""],          # the file is used as it is
        "config": {"prediction_length": benchmark.prediction_length, "prediction_offset": 0},
        "test_start": benchmark.test_start.isoformat(),
        "test_end": benchmark.test_end.isoformat(),
        "input_columns": manifest["input"]["columns"],
        "date_column": benchmark.date_column,
        "target_column": benchmark.target_column,
        "covariate_columns": [c for covariate in benchmark.covariates for c in covariate.output_columns()],
    }


def copy_as_draft(benchmark_id: int, created_by: int | None = None) -> int:
    """New draft with the same definition (the way to change a final benchmark); the copy is made by ``created_by``.

    Raises:
        NotFound: Unknown benchmark.
    """
    row = _row(benchmark_id)
    benchmark = replace(store.benchmark_from_row(row), prediction_length=None, data_commit=None)
    suffix = " (copy)"
    name = row["name"][: 200 - len(suffix)] + suffix
    return store.create_draft(name, row.get("description"), benchmark, created_by)
