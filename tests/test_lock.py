"""Tests for the advisory single-writer lock (review m13)."""

from __future__ import annotations

import json
import time

import pytest

from caldera.core.lock import SingleWriterLock, WriterLockHeld


def test_acquire_then_second_is_refused(tmp_path):
    lock = tmp_path / "writer.lock"
    a = SingleWriterLock(lock)
    a.acquire()
    b = SingleWriterLock(lock)
    with pytest.raises(WriterLockHeld):
        b.acquire()


def test_release_allows_reacquire(tmp_path):
    lock = tmp_path / "writer.lock"
    a = SingleWriterLock(lock)
    a.acquire()
    a.release()
    SingleWriterLock(lock).acquire()  # no raise


def test_stale_lock_is_taken_over(tmp_path):
    lock = tmp_path / "writer.lock"
    lock.write_text(json.dumps({"host": "old", "pid": 1, "ts": time.time() - 1000}))
    SingleWriterLock(lock, stale_seconds=90).acquire()  # stale → taken over, no raise


def test_heartbeat_refreshes_timestamp(tmp_path):
    lock = tmp_path / "writer.lock"
    a = SingleWriterLock(lock)
    a.acquire()
    first = json.loads(lock.read_text())["ts"]
    time.sleep(0.01)
    a.heartbeat()
    assert json.loads(lock.read_text())["ts"] > first
