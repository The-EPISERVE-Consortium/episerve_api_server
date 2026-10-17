"""Tests for the lakeFS helpers that pin and read dataset versions (no network)."""

from types import SimpleNamespace

import pytest

from app.clients import lakefs as lakefs_client

COMMIT = "0123abcd" * 8
PATH = "94/93/81/Q9493814122238/components/curve.parquet"


class _Reader:
    def __init__(self, data):
        self._data = data

    def read(self, read_bytes=None):
        return self._data if read_bytes is None else self._data[:read_bytes]


class _Ref:
    def __init__(self, repo, ref):
        self._repo, self._ref = repo, ref

    def object(self, path):
        self._repo.reads.append((self._ref, path))
        data = self._repo.files[path]
        return SimpleNamespace(reader=lambda: _Reader(data))


class _Repo:
    last = None

    def __init__(self, name, client=None):
        self.name, self.reads = name, []
        self.files = {PATH: b"PAR1-data"}
        _Repo.last = self

    def branch(self, name):
        return SimpleNamespace(head=SimpleNamespace(id=f"{COMMIT}"), name=name)

    def ref(self, ref):
        return _Ref(self, ref)


@pytest.fixture(autouse=True)
def fake_lakefs(monkeypatch):
    monkeypatch.setattr(lakefs_client, "_client", lambda: None)
    monkeypatch.setattr(lakefs_client.lakefs, "Repository", _Repo)


def test_read_component_uses_sharded_path_at_the_given_commit():
    data = lakefs_client.read_processed_component(COMMIT, "Q9493814122238", "curve.parquet", 1024)
    assert data == b"PAR1-data"
    assert _Repo.last.reads == [(COMMIT, PATH)]


def test_read_component_rejects_oversized_objects():
    with pytest.raises(ValueError, match="size limit"):
        lakefs_client.read_processed_component(COMMIT, "Q9493814122238", "curve.parquet", 4)


@pytest.mark.parametrize("commit,qid,filename", [
    ("not-a-commit", "Q9493814122238", "curve.parquet"),
    (COMMIT, "Q1/../x", "curve.parquet"),
    (COMMIT, "9493814122238", "curve.parquet"),
    (COMMIT, "Q9493814122238", "../curve.parquet"),
    (COMMIT, "Q9493814122238", "a/b.parquet"),
    (COMMIT, "Q9493814122238", ".."),
    ("deadbeef", "Q9493814122238", "curve.parquet"),            # short hex could alias a branch name
])
def test_read_component_rejects_unsafe_arguments(commit, qid, filename):
    with pytest.raises(ValueError):
        lakefs_client.read_processed_component(commit, qid, filename, 1024)


class _FakeBenchmarksRepo:
    def __init__(self):
        self.files, self.commits, self.reads = {}, [], []
        self.head = SimpleNamespace(id=COMMIT)

    def branch(self, name):
        repo = self

        class Branch:
            head = repo.head

            def object(self, path):
                return SimpleNamespace(upload=lambda data, mode, pre_sign: repo.files.__setitem__(path, data))

            def commit(self, message):
                repo.commits.append(message)

        return Branch()

    def ref(self, commit):
        return _Ref(self, commit)


def test_benchmark_files_are_written_below_the_id_and_read_back_at_the_commit(monkeypatch):
    repo = _FakeBenchmarksRepo()
    monkeypatch.setattr(lakefs_client.lakefs, "Repository", lambda name, client=None: repo)
    monkeypatch.setattr(lakefs_client, "_client", lambda: None)
    branch, commit = lakefs_client.write_benchmark_files(12, {"input.parquet": b"in", "reference.parquet": b"ref"})
    assert (branch, commit) == ("main", COMMIT) and len(repo.commits) == 1
    assert repo.files == {"12/input.parquet": b"in", "12/reference.parquet": b"ref"}
    assert lakefs_client.read_benchmark_file(COMMIT, "12/input.parquet", 100) == b"in"
    assert repo.reads == [(COMMIT, "12/input.parquet")]
    with pytest.raises(ValueError, match="size limit"):
        lakefs_client.read_benchmark_file(COMMIT, "12/input.parquet", 1)


@pytest.mark.parametrize("commit,path", [("main", "12/input.parquet"), (COMMIT, "../input.parquet"),
                                         (COMMIT, "12/../x"), (COMMIT, "x/input.parquet"), (COMMIT, "12/a/b")])
def test_benchmark_files_refuse_unsafe_arguments(monkeypatch, commit, path):
    monkeypatch.setattr(lakefs_client, "_client", lambda: (_ for _ in ()).throw(AssertionError("no client")))
    with pytest.raises(ValueError):
        lakefs_client.read_benchmark_file(commit, path, 10)
    with pytest.raises(ValueError):
        lakefs_client.write_benchmark_files(12, {"../x": b""})

