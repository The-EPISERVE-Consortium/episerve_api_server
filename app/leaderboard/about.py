"""Generated description of a benchmark: the data it uses, how it scores and which runs it accepts."""

from app.leaderboard.metrics import ALPHA

# (name, meaning, which value is better) in the order of metrics.METRICS
METRIC_DOCS = [
    ("MAE", "Mean absolute error: the mean of |forecast − observed| over the scored dates.", "lower"),
    ("RMSE", "Root mean squared error: the square root of the mean of (forecast − observed)².", "lower"),
    ("80% coverage",
     "Share of the scored dates whose observed value lies between q10 and q90. Needs the q10 and q90 columns "
     "on every scored date.", "closest to 80%"),
    ("interval score",
     f"Mean over the scored dates of (q90 − q10) + {2 / ALPHA:g} · max(q10 − observed, 0) + "
     f"{2 / ALPHA:g} · max(observed − q90, 0), i.e. the interval width plus a penalty of 2/α = {2 / ALPHA:g} "
     f"(α = {ALPHA:g}) for every unit the observation lies outside. Needs q10 and q90 on every scored date.",
     "lower"),
]


def is_baseline(model_name: str) -> bool:
    """Reference models are recognised by 'baseline' in their name."""
    return "baseline" in model_name.lower()


def _filter_text(row: dict) -> str:
    return f"{row['filter_column']} = {row.get('filter_value') or ''}" if row.get("filter_column") else "none"


def description_text(row: dict) -> str:
    """The benchmark's own description, or a sentence generated from its definition."""
    if row.get("description"):
        return row["description"]
    subject = f"{row['target_column']} for {_filter_text(row)}" if row.get("filter_column") else row["target_column"]
    return f"Forecast of {subject} over a test window of {row['test_start']} → {row['test_end']}."


def overview_facts(row: dict) -> list[tuple[str, str]]:
    """What is forecast and how much has been run; the data details are on the Data tab."""
    series = f"{row['target_column']} · {_filter_text(row)}" if row.get("filter_column") else row["target_column"]
    length = row.get("prediction_length")
    facts = [("Series", series), ("Forecast horizon", f"{length} steps" if length else "—")]
    if row.get("entry_count") is not None:
        facts.append(("Runs", str(row["entry_count"])))
    return facts


def training_facts(row: dict) -> list[tuple[str, str]]:
    """What a model sees: the dataset and the input range."""
    start = row.get("history_start") or "first date"
    end = row.get("history_end") or "last date before the test start"
    return [
        ("Dataset", row["dataset_qid"]),
        ("Component", row.get("component_name") or "—"),
        ("Date column", row["date_column"]),
        ("Input range", f"{start} → {end}"),
        ("Input file name", row.get("input_filename") or "input.parquet"),
    ]


def evaluation_facts(row: dict) -> list[tuple[str, str]]:
    """What forecasts are scored against and the data version it is pinned to."""
    points = row.get("truth_points")
    return [
        ("Test window", f"{row['test_start']} → {row['test_end']}"),
        ("Observations in the test window", str(points) if points is not None else "—"),
        ("Data commit", row.get("data_commit") or "—"),
        ("Pinned", str(row.get("finalized_at") or "—")),
    ]


def entry_rules(row: dict) -> list[str]:
    """What a model run must satisfy to be added to the leaderboard."""
    commit = (row.get("data_commit") or "")[:12] or "the pinned data commit"
    rules = [
        "The run succeeded and has this benchmark's dataset as its input.",
        f"It read exactly one input of that dataset at the benchmark's commit ({commit}); the run's provenance "
        "must show it.",
        f"Its history ends before the test start ({row['test_start']}); the transformation SQL needs a single "
        "upper date bound on the date column (a DATE literal; OR, NOT, UNION and INTERVAL are not scorable).",
    ]
    if row.get("filter_column"):
        rules.append(f"The SQL contains the filter {_filter_text(row)} as an equality condition.")
    rules.append(f"It forecasts every observed date from {row['test_start']} to {row['test_end']}.")
    return rules
