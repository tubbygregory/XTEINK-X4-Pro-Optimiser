"""Cancellable I/O and private staging files."""

from contextlib import contextmanager
from hashlib import sha256
import os
from pathlib import Path
import tempfile


CHUNK_SIZE = 1024 * 1024


class OptimisationError(Exception):
    """An input or a destination cannot be used safely."""


class Cancelled(Exception):
    """Raised at a cancellation boundary."""


def check_cancel(cancel):
    if cancel is not None and cancel.is_set():
        raise Cancelled()


def chunks(source, cancel=None, limit=None):
    total = 0
    while True:
        check_cancel(cancel)
        block = source.read(CHUNK_SIZE)
        if not block:
            break
        total += len(block)
        if limit is not None and total > limit:
            raise OptimisationError("The input exceeds the supported size limit.")
        yield block


def stream_copy(source, destination, cancel=None, limit=None):
    for block in chunks(source, cancel, limit):
        destination.write(block)


def hash_file(path, cancel=None):
    digest = sha256()
    with open(path, "rb") as stream:
        for block in chunks(stream, cancel):
            digest.update(block)
    return digest.hexdigest()


@contextmanager
def atomic_output(destination, cancel=None):
    """Yield a same-directory staging path and publish on successful exit."""
    destination = Path(destination)
    fd, name = tempfile.mkstemp(prefix=".xteink-", suffix=".part", dir=destination.parent)
    os.close(fd)
    staging = Path(name)
    try:
        yield staging
        check_cancel(cancel)
        with staging.open("rb+") as stream:
            os.fsync(stream.fileno())
        check_cancel(cancel)
        os.replace(staging, destination)
    finally:
        staging.unlink(missing_ok=True)
