"""Validated, content-addressed copies outside the Calibre library."""

from dataclasses import asdict
from hashlib import sha256
import json
from pathlib import Path
import re

from . import ENGINE_VERSION
from .engine import Statistics, optimise_epub
from .io_utils import atomic_output, check_cancel, hash_file
from .validation import validator_identity


CACHE_ENTRY = re.compile(r"[0-9a-f]{64}\.(?:epub|sha256)")


def cache_key(content_hash, options, epubcheck=()):
    key = {
        "content": content_hash, "version": ENGINE_VERSION,
        "options": asdict(options),
        "validator": validator_identity(epubcheck) if options.validate_epubcheck else (),
    }
    return sha256(json.dumps(key, sort_keys=True, separators=(",", ":")).encode("ascii")).hexdigest()


class Cache:
    def __init__(self, directory):
        self.directory = Path(directory)

    def get_or_optimise(self, source, content_hash, options, *, epubcheck=(), **kwargs):
        cancel = kwargs.get("cancel")
        check_cancel(cancel)
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        key = cache_key(content_hash, options, epubcheck)
        destination = self.directory / f"{key}.epub"
        checksum = self.directory / f"{key}.sha256"
        if self._intact(destination, checksum, cancel):
            return destination, Statistics(Path(source).stat().st_size, destination.stat().st_size, cache_hit=True)
        stats = optimise_epub(source, destination, options, epubcheck=epubcheck, **kwargs)
        digest = hash_file(destination, cancel)
        with atomic_output(checksum, cancel) as staged:
            staged.write_bytes(digest.encode("ascii"))
        return destination, stats

    @staticmethod
    def _intact(destination, checksum, cancel):
        if any(path.is_symlink() or not path.is_file() for path in (destination, checksum)):
            return False
        try:
            with checksum.open("rb") as stream:
                digest = stream.read(65)
            return len(digest) == 64 and digest == hash_file(destination, cancel).encode("ascii")
        except FileNotFoundError:
            return False

    def clear(self):
        if not self.directory.is_dir():
            return 0, 0
        count = size = 0
        for entry in self.directory.iterdir():
            if CACHE_ENTRY.fullmatch(entry.name) and not entry.is_symlink() and entry.is_file():
                try:
                    length = entry.stat().st_size
                    entry.unlink()
                except FileNotFoundError:
                    continue
                if entry.suffix == ".epub":
                    count += 1
                    size += length
        return count, size
