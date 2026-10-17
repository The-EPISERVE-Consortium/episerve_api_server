"""Which columns of the input file a run used."""

from app.benchmarks.columns import used_columns

MANIFEST = {"input": {"columns": ["Meldedatum", "Inzidenz_7-Tage", "wetter__temperature_2m_mean_C"]}}


def test_without_sql_a_run_uses_every_column():
    assert used_columns(MANIFEST, None) == MANIFEST["input"]["columns"]
    assert used_columns(MANIFEST, [""]) == MANIFEST["input"]["columns"]
    assert used_columns(MANIFEST, ["  "]) == MANIFEST["input"]["columns"]


def test_a_selection_gives_the_columns_the_sql_returns():
    sql = 'SELECT "Meldedatum", "Inzidenz_7-Tage" FROM df;'
    assert used_columns(MANIFEST, [sql]) == ["Meldedatum", "Inzidenz_7-Tage"]
    assert used_columns(MANIFEST, ['SELECT * EXCLUDE ("wetter__temperature_2m_mean_C") FROM df WHERE Meldedatum >= DATE \'2022-01-01\'']) \
        == ["Meldedatum", "Inzidenz_7-Tage"]
    assert used_columns(MANIFEST, ['SELECT "Inzidenz_7-Tage" AS y, "Meldedatum" AS ds FROM df']) == ["y", "ds"]


def test_sql_that_does_not_work_or_reaches_outside_gives_none():
    assert used_columns(MANIFEST, ["SELECT nope FROM df"]) is None
    assert used_columns(MANIFEST, ["SELECT * FROM read_csv('/etc/passwd')"]) is None
    assert used_columns(MANIFEST, ["DROP TABLE df"]) is None
