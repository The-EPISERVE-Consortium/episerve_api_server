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


_BETTER = {"lower": "Lower is better.", "closest to 80%": "The closer to 80% the better."}

# Hover help for the column titles of the leaderboard and of the candidates table, by column name.
COLUMN_HELP = {
    "rank": "Position by the chosen ranking metric (see Rank by).",
    "model": "The forecast model. Reference models carry a baseline badge.",
    "tag": "The version (image tag) of the model that made the run.",
    "run": "The model run (QID). In the leaderboard it links to the details of the run.",
    "date": "When the run was made.",
    "params": "The configuration the model was run with.",
    "history": "The window of the series the model was given as input (first date → last date, '…' if it has no "
               "start). The test window starts after it.",
    "scored": "Dates scored / dates in the test window. A run is only accepted when it forecasts every date.",
    **{key: f"{meaning} {_BETTER.get(better, '')}".strip()
       for key, (_, meaning, better) in zip(["mae", "rmse", "coverage", "interval_score"], METRIC_DOCS)},
}


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
    if row.get("created_by_name"):
        facts.append(("Created by", row["created_by_name"]))
    if row.get("entry_count") is not None:
        facts.append(("Runs", str(row["entry_count"])))
    return facts
