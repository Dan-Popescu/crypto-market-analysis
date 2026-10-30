"""Fail-fast cooperative process lock for named datasets and ordinary store jobs.

All participating writers must hold this guard before construction/recovery and
through their final write. Direct HistoryDataset clients are caller-owned. The
stable lock file is deliberately never unlinked: removing it permits two locks
on different inodes. Process exit, including an abrupt kill, releases the lock.
"""
from __future__ import annotations

import errno
import os
import stat
from contextlib import contextmanager
from pathlib import Path


class DatasetBusy(RuntimeError):
    """Another cooperating process already holds this store's writer lock."""


@contextmanager
def dataset_writer_lock(database):
    """Hold an OS writer lock without waits, directory creation or recovery.

    The database and named identity must already exist. The persistent lock file
    contains at most one byte and no PID, paths, credentials or other metadata.
    """
    database = Path(database).expanduser().resolve()
    if not database.is_file() or not (database.parent / "dataset.json").is_file():
        raise ValueError("Writer locking requires an existing named dataset store.")
    with _writer_lock(database.parent):
        yield


@contextmanager
def store_writer_lock(database, *, create=False):
    """Serialize cooperating ordinary-store jobs before initialization.

    Only explicit apply callers may create the parent. Ordinary collectors do
    not participate in this guard; their transactions remain SQLite-managed.
    """
    database = Path(database).expanduser().resolve()
    if (database.parent / "dataset.json").exists():
        raise ValueError("Ordinary store jobs cannot write named history datasets.")
    if create:
        database.parent.mkdir(parents=True, exist_ok=True)
    elif not database.is_file():
        raise ValueError("Store writer locking requires an existing database.")
    with _writer_lock(database.parent):
        yield


@contextmanager
def _writer_lock(directory):
    lock_path = directory / ".history-writer.lock"
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(lock_path, flags, 0o600)
    acquired = False
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode) or lock_path.is_symlink():
            raise ValueError("Dataset writer lock must be a regular local file.")
        try:
            if os.name == "nt":
                import msvcrt
                if os.fstat(descriptor).st_size == 0:
                    os.write(descriptor, b"\0")
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                raise DatasetBusy("Another process is writing this store.") from None
            raise
        yield
    finally:
        try:
            if acquired:
                if os.name == "nt":
                    import msvcrt
                    os.lseek(descriptor, 0, os.SEEK_SET)
                    msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)
