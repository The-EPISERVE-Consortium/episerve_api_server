"""The rule that decides whether a model run can be an entry of a benchmark.

Every benchmark has a generated input file (``materialize``). A run qualifies when it read exactly that file, in the
version recorded in the benchmark's manifest, and nothing else.
"""

from dataclasses import dataclass
from datetime import date

from app.benchmarks.definition import Benchmark, input_uri  # noqa: F401  (Benchmark is re-exported)


@dataclass(frozen=True)
class HistoryWindow:
    """Dates of input history a run used; ``start`` is None when unbounded."""

    start: date | None
    end: date


@dataclass(frozen=True)
class Qualification:
    """Outcome of checking one run against a benchmark.

    ``relevant`` is False when the run did not read this benchmark's input (not listed).
    A non-empty ``reason`` means it is listed as not scorable.
    """

    relevant: bool
    reason: str | None = None
    window: HistoryWindow | None = None


def _short(commit: str | None) -> str:
    return commit[:12] if commit else "unknown"


def qualify_run(run: dict, manifest: dict) -> Qualification:
    """Decide whether a catalog run can be an entry of the benchmark with this manifest.

    The run must have read exactly the benchmark's generated input file in the stored version and nothing else; the
    catalog record lists what a run read (``input_sources``).
    """
    sources = run.get("input_sources") or []
    base = input_uri(manifest, versioned=False)
    ours = [source for source in sources if source.split("?")[0] == base]
    if not ours:
        return Qualification(relevant=False)
    if run.get("status") != "success":
        return Qualification(True, f"run status is {run.get('status') or 'unknown'}, not success")
    if len(sources) != 1:
        return Qualification(True, "the run read other inputs besides the benchmark's input file; "
                                   "a benchmark run must use exactly that file")
    if ours[0] != input_uri(manifest):
        read = ours[0].partition("?version=")[2]
        return Qualification(True, f"the input file was read at version {_short(read or None)}, the benchmark's "
                                   f"is {_short(manifest['commit'])}")
    first, last = manifest["input"].get("first_date"), manifest["input"].get("last_date")
    return Qualification(True, None, HistoryWindow(date.fromisoformat(first) if first else None,
                                                   date.fromisoformat(last)))

