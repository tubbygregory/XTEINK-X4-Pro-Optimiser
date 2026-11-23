"""Read an OCF ZIP without extracting it and stream safe replacements."""

from contextlib import contextmanager
import os
import re
import stat
from struct import Struct
from unicodedata import normalize
from zipfile import BadZipFile, LargeZipFile, ZIP_DEFLATED, ZIP_STORED, ZipFile, ZipInfo
from zlib import error as ZlibError

from .io_utils import OptimisationError, check_cancel, chunks, stream_copy


MIMETYPE = b"application/epub+zip"
MAX_ARCHIVE = 768 * 1024 * 1024
MAX_TOTAL = 1024 * 1024 * 1024
MAX_MEMBER = 128 * 1024 * 1024
MAX_ENTRIES = 20_000
MAX_RATIO = 1_000
MAX_DIRECTORY = 32 * 1024 * 1024
END_RECORD = Struct('<4s4H2LH')


def path_key(name):
    return normalize("NFC", name).casefold()


def reserved_component(name):
    return bool(re.fullmatch(
        r"(?i:CON|PRN|AUX|NUL|CONIN\$|CONOUT\$|COM[1-9¹²³]|LPT[1-9¹²³])",
        name.split(".", 1)[0].rstrip(" "),
    ))


def check_member_name(name):
    if len(name) > 1024 or re.search(r'[\x00-\x1f\x7f\\:<>"|?*]', name):
        raise OptimisationError(f"Unsafe EPUB entry name: {name!r}.")
    for part in name.removesuffix("/").split("/"):
        if not part or part in (".", "..") or part.endswith((" ", ".")) or reserved_component(part):
            raise OptimisationError(f"Unsafe EPUB entry path: {name!r}.")


@contextmanager
def open_archive(path):
    try:
        with open(path, "rb") as source:
            size = os.fstat(source.fileno()).st_size
            if size > MAX_ARCHIVE:
                raise OptimisationError("The EPUB exceeds the compressed archive size limit.")
            # Limit the central directory before ZipFile allocates and parses it.
            source.seek(max(0, size - 65_535 - END_RECORD.size))
            tail = source.read()
            end = tail.rfind(b"PK\x05\x06")
            if end < 0 or len(tail) - end < END_RECORD.size:
                raise BadZipFile("End of central directory not found")
            _, disk, first_disk, disk_entries, count, length, offset, comment = END_RECORD.unpack_from(tail, end)
            if len(tail) - end != END_RECORD.size + comment or disk or first_disk or disk_entries != count:
                raise BadZipFile("Invalid or multi-disk central directory")
            locator = size - len(tail) + end - 20
            if locator >= 0:
                source.seek(locator)
                if source.read(4) == b"PK\x06\x07":
                    raise OptimisationError("ZIP64 EPUB containers are unsupported.")
            if count > MAX_ENTRIES or length > MAX_DIRECTORY or length + offset > size:
                raise OptimisationError("The EPUB central directory exceeds supported limits.")
            with ZipFile(source) as archive:
                yield archive
    except (BadZipFile, LargeZipFile, EOFError, NotImplementedError, ZlibError) as error:
        raise OptimisationError(f"Invalid or unsupported EPUB ZIP: {error}") from error


def inspect_archive(archive, normalise=False, cancel=None):
    entries = archive.infolist()
    if not entries or len(entries) > MAX_ENTRIES:
        raise OptimisationError("The EPUB has an unsupported number of ZIP entries.")

    files = {}
    paths = {}
    directories = {}
    explicit = set()
    total = 0
    for entry in entries:
        check_cancel(cancel)
        name = entry.orig_filename
        check_member_name(name)
        canonical = name.removesuffix("/")
        folded = path_key(canonical)
        if folded in explicit:
            raise OptimisationError(f"Duplicate or ambiguous EPUB entry: {name!r}.")
        explicit.add(folded)
        mode = stat.S_IFMT(entry.external_attr >> 16)
        expected = stat.S_IFDIR if entry.is_dir() else stat.S_IFREG
        if (mode and mode != expected) or entry.external_attr & 0x400 or (entry.is_dir() and entry.file_size):
            raise OptimisationError(f"Unsupported special EPUB entry: {name!r}.")
        if entry.flag_bits & 1:
            raise OptimisationError("This EPUB is encrypted or DRM-protected.")
        if entry.compress_type not in (ZIP_STORED, ZIP_DEFLATED):
            raise OptimisationError(f"Unsupported ZIP compression in {name!r}.")
        total += entry.file_size
        if entry.file_size > MAX_MEMBER or total > MAX_TOTAL or (
            entry.file_size >= 2 * 1024 * 1024
            and entry.file_size > MAX_RATIO * max(1, entry.compress_size)
        ):
            raise OptimisationError(f"EPUB expansion limit exceeded at {name!r}.")

        parts = canonical.split("/")
        last_parent = len(parts) if entry.is_dir() else len(parts) - 1
        for depth in range(1, last_parent + 1):
            parent = "/".join(parts[:depth])
            key = path_key(parent)
            if key in paths or directories.setdefault(key, parent) != parent:
                raise OptimisationError(f"Conflicting EPUB directory: {parent!r}.")
        if not entry.is_dir():
            if folded in directories:
                raise OptimisationError(f"Conflicting EPUB file: {name!r}.")
            paths[folded] = canonical
            files[name] = entry

    if "meta-inf/encryption.xml" in paths or "meta-inf/rights.xml" in paths:
        raise OptimisationError("This EPUB declares encryption, font obfuscation or DRM rights.")
    if "meta-inf/signatures.xml" in paths:
        raise OptimisationError("This EPUB is digitally signed. Optimisation could invalidate its signature.")

    mimetype = files.get("mimetype")
    if mimetype is None or mimetype.file_size != len(MIMETYPE) or archive.read(mimetype) != MIMETYPE:
        raise OptimisationError("Missing or invalid EPUB mimetype entry.")
    # Check the local header as its extra field can differ from the directory record.
    saved_position = archive.fp.tell()
    try:
        archive.fp.seek(mimetype.header_offset)
        header = archive.fp.read(30)
    finally:
        archive.fp.seek(saved_position)
    correct = (
        entries[0] is mimetype and mimetype.header_offset == 0
        and mimetype.compress_type == ZIP_STORED and not mimetype.extra and len(header) == 30
        and header[:4] == b"PK\x03\x04" and header[8:10] == b"\x00\x00"
        and header[28:30] == b"\x00\x00"
    )
    if not correct and not normalise:
        raise OptimisationError("The mimetype entry must be first, uncompressed and have no ZIP extra fields.")
    return files, not correct


def check_contents(archive, entries, cancel=None):
    for entry in entries.values():
        with archive.open(entry) as source:
            for _ in chunks(source, cancel, limit=entry.file_size):
                pass


def write_archive(archive, entries, changes, destination, cancel=None):
    with ZipFile(destination, "w", compression=ZIP_DEFLATED, compresslevel=6) as output:
        output.writestr("mimetype", MIMETYPE, compress_type=ZIP_STORED)
        for name, entry in entries.items():
            check_cancel(cancel)
            if name == "mimetype":
                continue
            with archive.open(entry) as source:
                info = ZipInfo(name, date_time=entry.date_time)
                info.compress_type = entry.compress_type
                info.external_attr = (stat.S_IFREG | 0o644) << 16
                replacement = changes.get(name)
                if replacement is None:
                    with output.open(info, "w") as target:
                        stream_copy(source, target, cancel, limit=entry.file_size)
                else:
                    with replacement.open("rb") as modified, output.open(info, "w") as target:
                        stream_copy(modified, target, cancel, limit=MAX_MEMBER)
