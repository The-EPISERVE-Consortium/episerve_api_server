"""Database cache behavior with explicit connection scripts."""

import json

import pytest

from app.forecast_preview import cache
from tests.fakes import FakeConn


def connect(monkeypatch, *rows):
    """Provide a fake database result per statement.

    Args:
        monkeypatch: Fixture replacing database access.
        rows: Successive cursor result rows.

    Returns:
        Observable fake connection.
    """
    conn = FakeConn([(item, len(item), None) for item in rows])
    monkeypatch.setattr(cache.db, 'connect', lambda: conn)
    return conn


def test_cache_hit_skips_generation(monkeypatch):
    """Read exact version data without locks or artifact reconstruction.

    Args:
        monkeypatch: Fixture supplying a populated cache.
    """
    conn = connect(monkeypatch, [{'payload': '{"value": 3}'}])
    result = cache.get_or_create('Q1', 'a' * 64, lambda: pytest.fail('cache hit must not rebuild'))
    assert result == {'value': 3, 'cached': True}
    assert conn.executed[0][1] == ('Q1', 'a' * 64, cache.FORMAT_VERSION)
    assert conn.closed and len(conn.executed) == 1


def test_miss_is_rechecked_under_lock_and_persisted(monkeypatch):
    """Publish only a completed preview and release the shared lock.

    Args:
        monkeypatch: Fixture supplying an empty cache and available lock.
    """
    conn = connect(monkeypatch, [], [{'acquired': 1}], [], [], [])
    result = cache.get_or_create('Q1', 'b' * 64, lambda: {'value': 4})
    assert result == {'value': 4, 'cached': False}
    assert 'GET_LOCK' in conn.executed[1][0]
    assert json.loads(conn.executed[3][1][-1]) == {'value': 4}
    assert 'RELEASE_LOCK' in conn.executed[-1][0] and conn.closed


def test_another_worker_can_populate_cache_before_lock_is_acquired(monkeypatch):
    """Recheck after ending the old transaction snapshot.

    Args:
        monkeypatch: Fixture supplying a late cache insert.
    """
    conn = connect(monkeypatch, [], [{'acquired': 1}], [{'payload': '{"value": 5}'}], [])
    assert cache.get_or_create('Q1', 'a' * 64, lambda: pytest.fail('duplicate generation'))['cached']
    assert not any('INSERT' in sql for sql, _ in conn.executed)


def test_busy_lock_returns_retryable_failure(monkeypatch):
    """Avoid duplicate generation when another process is already working.

    Args:
        monkeypatch: Fixture denying the lock.
    """
    conn = connect(monkeypatch, [], [{'acquired': 0}])
    with pytest.raises(cache.PreviewBusy):
        cache.get_or_create('Q1', 'a' * 64, lambda: pytest.fail('busy'))
    assert conn.closed


@pytest.mark.parametrize('mode', ['failure', 'incomplete', 'oversized'])
def test_incomplete_or_failed_builds_are_not_cached(monkeypatch, mode):
    """Release locks without inserting unsuccessful or excessive previews.

    Args:
        monkeypatch: Fixture supplying an empty cache.
        mode: Failure condition to exercise.
    """
    conn = connect(monkeypatch, [], [{'acquired': 1}], [], [])

    def build():
        """Build the selected failure or incomplete result.

        Returns:
            Uncacheable preview or oversized data.

        Raises:
            RuntimeError: Generation failed.
        """
        if mode == 'failure':
            raise RuntimeError('generation failed')
        return {'cacheable': False} if mode == 'incomplete' else {'value': 'a' * cache.MAX_PAYLOAD_BYTES}

    if mode == 'incomplete':
        assert not cache.get_or_create('Q1', 'a' * 64, build)['cached']
    else:
        with pytest.raises((RuntimeError, ValueError)):
            cache.get_or_create('Q1', 'a' * 64, build)
    assert not any('INSERT' in sql for sql, _ in conn.executed)
    assert 'RELEASE_LOCK' in conn.executed[-1][0] and conn.closed
