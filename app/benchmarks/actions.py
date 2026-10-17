"""Design, check, finalize and retire benchmarks; build run specs.

A benchmark is edited as a draft and frozen by ``finalize``, which pins the current lakeFS
head commit of ``data-processed``: the reference data is read at that commit and the run
spec makes model runs read their input at the same commit.
"""

from dataclasses import dataclass, field, replace
from datetime import date, timedelta

from app.benchmarks import store
from app.benchmarks.definition import (
    Benchmark, DefinitionError, derive_horizon, input_sql, normalize_filters, validate,
)
from app.benchmarks.errors import Conflict, Forbidden, NotDraft, NotFound  # noqa: F401  (re-exported)
from app.clients import ckan as ckan_client
from app.clients import lakefs as lakefs_client
from lakefs.exceptions import NotFoundException

from app.config import settings
from app.dataset_preview import MAX_DOWNLOAD_BYTES
from app.leaderboard import loaders
from app.leaderboard.entries import has_equality_filter, parse_history_window
from app.leaderboard.loaders import LoadError
from app.users import User

_EDITABLE = {
    "name", "description", "dataset_qid", "target_column", "date_column", "test_start", "test_end",
    "filters", "history_start", "history_end", "input_filename",
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
    input_sql: str | None = None


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
    )
    validate(benchmark, name)
    _dataset(dataset_qid)
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
    merged = replace(current, **changes)
    validate(merged, name)
    if "dataset_qid" in changes:
        _dataset(merged.dataset_qid)
    if not store.update_draft(benchmark_id, name.strip(), description, merged):
        raise NotFound(f"Benchmark {benchmark_id} not found")


def _evaluate(benchmark: Benchmark, dataset: dict, commit: str) -> CheckReport:
    """Read the data at ``commit`` and report problems, warnings and the derived values."""
    report = CheckReport(ok=False, data_commit=commit)
    components = dataset.get("components") or []
    if not components:
        report.problems.append("The dataset has no downloadable component.")
        return report
    report.component_name = components[0]["name"]
    try:
        data = lakefs_client.read_processed_component(commit, dataset["qid"], report.component_name, MAX_DOWNLOAD_BYTES)
    except NotFoundException:
        report.problems.append("The dataset file does not exist at the pinned commit.")
        return report
    except ValueError as exc:
        report.problems.append(str(exc))
        return report
    try:
        truth = loaders.parse_truth(data, benchmark)
    except LoadError as exc:
        report.problems.append(str(exc))
        return report
    report.truth_points = len(truth)
    history_end = benchmark.history_end
    try:
        if history_end is None:
            history_end = loaders.last_observed_date(data, benchmark, benchmark.test_start - timedelta(days=1))
            report.last_history_date = history_end
        elif history_end >= benchmark.test_start:
            raise LoadError("The history must end before the test start.")
        resolved = replace(benchmark, history_end=history_end)
        report.history_end = history_end
        if benchmark.history_end is not None:
            report.last_history_date = loaders.last_observed_date(data, resolved, history_end)
    except LoadError as exc:
        report.problems.append(str(exc))
        return report
    if benchmark.history_start is not None and benchmark.history_start > report.last_history_date:
        report.problems.append(
            "There are no observations in the input range (history start is after the last observation "
            "on or before the history end)."
        )
        return report
    try:
        horizon = derive_horizon([d.date() for d in truth["date"]], report.last_history_date)
    except DefinitionError as exc:
        report.problems.append(str(exc))
        return report
    report.prediction_length, report.frequency_days = horizon.prediction_length, horizon.frequency_days
    if not horizon.regular:
        report.warnings.append(
            "The observed dates are not evenly spaced; one forecast grid may not hit every observed date."
        )
    report.input_sql = input_sql(resolved)
    window = parse_history_window(report.input_sql, benchmark.date_column)
    filter_ok = all(has_equality_filter(report.input_sql, column, value) for column, value in benchmark.filters)
    if window is None or window.end != history_end or not filter_ok:
        report.problems.append(
            "The generated input SQL is not accepted by the qualification rules "
            "(a filter value or column name may contain a reserved word such as OR, NOT or UNION)."
        )
    report.ok = not report.problems
    return report


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
    done = store.finalize(
        benchmark_id, data_commit=commit, component_name=report.component_name, truth_points=report.truth_points,
        prediction_length=report.prediction_length, history_end=report.history_end,
    )
    if not done:
        raise NotFound(f"Benchmark {benchmark_id} not found")


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
    url = (
        f"{settings.doip_url.rstrip('/')}/doip/retrieve/{benchmark.dataset_qid}/"
        f"{row['component_name']}?version={benchmark.data_commit}"
    )
    return {
        "benchmark_id": row["id"],
        "dataset_qid": benchmark.dataset_qid,
        "data_commit": benchmark.data_commit,
        "input_data_files": [[url, benchmark.input_filename]],
        "data_transformation_sql": [input_sql(benchmark)],
        "config": {"prediction_length": benchmark.prediction_length, "prediction_offset": 0},
        "test_start": benchmark.test_start.isoformat(),
        "test_end": benchmark.test_end.isoformat(),
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
