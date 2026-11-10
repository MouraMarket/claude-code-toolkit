#!/usr/bin/env python3
"""The thin platform layer.

Platform-specific locking primitives live here, and nowhere else -- every
caller reaches them only through `exclusive_lock`, so a POSIX-only or a
Windows-only bug has exactly one place to hide. `fcntl` and `msvcrt` are
imported inside their own platform branch, never at module level, so
importing this module never fails on either OS.
"""

import contextlib
import errno
import os
import time


@contextlib.contextmanager
def exclusive_lock(path, timeout=0.0):
    """Advisory, exclusive, cross-process lock on a sidecar file at `path`.

    Opens its own file descriptor on `path` (creating it if needed) and
    yields whether the lock was acquired -- never whether the `with` block
    ran, which happens either way. `timeout <= 0` makes exactly one attempt;
    a positive timeout retries every 0.05s until `time.monotonic()` passes
    the deadline. The file is never deleted or replaced, on either the
    acquire or the release path, so a second handle on the same path always
    sees the same file.

    An `OSError` opening `path` (e.g. its parent is not a directory)
    propagates -- the caller decides whether that is fatal.
    """
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    acquired = False
    try:
        deadline = time.monotonic() + timeout
        acquired = _try_lock(fd)
        while not acquired and timeout > 0 and time.monotonic() < deadline:
            time.sleep(0.05)
            acquired = _try_lock(fd)
        yield acquired
    finally:
        if acquired:
            try:
                _unlock(fd)
            except Exception:
                pass
        try:
            os.close(fd)
        except Exception:
            pass


def _try_lock(fd):
    """One non-blocking attempt at the whole-file exclusive lock on `fd`.

    POSIX: `fcntl.flock`, whose `BlockingIOError` (another holder) means
    False. Windows: `msvcrt.locking` on the first byte, whose `EACCES` or
    `EDEADLOCK` means the same thing. Any other `OSError` is not contention
    and propagates.
    """
    if os.name == "nt":
        import msvcrt
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EDEADLOCK):
                return False
            raise
        return True
    import fcntl
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    return True


def _unlock(fd):
    """Release the lock `_try_lock` took on `fd`.

    Raises on failure; `exclusive_lock` swallows it -- a lock that outlives
    its handle is a leak the OS cleans up on close anyway, not worth an
    exception from an exit path.
    """
    if os.name == "nt":
        import msvcrt
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        return
    import fcntl
    fcntl.flock(fd, fcntl.LOCK_UN)
