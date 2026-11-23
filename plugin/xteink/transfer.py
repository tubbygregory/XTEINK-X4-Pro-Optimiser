"""Confirmed filesystem destinations, portable filenames and atomic copies."""

from contextlib import ExitStack, contextmanager, suppress
from dataclasses import dataclass
import errno
import os
from pathlib import Path
import re
import stat
from string import Formatter
import sys
import unicodedata
from uuid import uuid4

from .archive import path_key, reserved_component
from .io_utils import OptimisationError, check_cancel, chunks, stream_copy


FIELDS = {"title", "authors", "series", "id"}
STRUCTURES = ("flat", "author", "series")
OVERWRITE = ("keep_both", "skip", "replace")
DEFAULT_TEMPLATE = "{title} - {authors}"
MAX_ATTEMPTS = 10_000


@dataclass(frozen=True)
class Book:
    id: int
    title: str
    authors: str
    series: str = ""
    first_author: str = ""


@dataclass(frozen=True)
class TransferOptions:
    template: str = DEFAULT_TEMPLATE
    structure: str = "author"
    overwrite: str = "keep_both"

    def __post_init__(self):
        validate_template(self.template)
        if self.structure not in STRUCTURES or self.overwrite not in OVERWRITE:
            raise ValueError("Invalid export folder or overwrite setting.")


@dataclass(frozen=True)
class TransferResult:
    path: Path
    status: str


def validate_template(template):
    if not isinstance(template, str) or not template.strip() or len(template) > 240:
        raise ValueError("Enter a filename template between 1 and 240 characters.")
    for _, field, spec, conversion in Formatter().parse(template):
        if field is not None and (field not in FIELDS or spec or conversion):
            raise ValueError("Use only {title}, {authors}, {series} and {id} in a filename template.")


def safe_component(value, default="Untitled"):
    value = unicodedata.normalize("NFC", value)
    value = "".join(
        "_" if char in '<>:"/\\|?*' or unicodedata.category(char) in {"Cc", "Cs", "Cf"} else char
        for char in value
    )
    value = value.strip().strip(".").strip()
    # Keep room for a collision suffix and long Windows destination paths.
    value = value.encode("utf-8")[:120].decode("utf-8", errors="ignore").rstrip(" .")
    value = value or default
    return f"_{value}" if reserved_component(value) else value


def relative_location(book, options):
    stem = options.template.format(title=book.title, authors=book.authors, series=book.series, id=book.id)
    stem = stem.strip()
    if stem.lower().endswith(".epub"):
        stem = stem[:-5]
    name = safe_component(stem, f"Book {book.id}")
    folder = ()
    if options.structure == "author":
        folder = (safe_component(book.first_author or book.authors, "Unknown author"),)
    elif options.structure == "series":
        folder = (safe_component(book.series, "No series"),)
    return folder, name


if os.name == "nt":
    import ctypes
    import msvcrt
    from ctypes import wintypes

    _kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    _create_file = _kernel.CreateFileW
    _create_file.argtypes = (
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    )
    _create_file.restype = wintypes.HANDLE
    _close_handle = _kernel.CloseHandle
    _close_handle.argtypes = (wintypes.HANDLE,)
    _close_handle.restype = wintypes.BOOL
    _handle_info = _kernel.GetFileInformationByHandleEx
    _handle_info.argtypes = (wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD)
    _handle_info.restype = wintypes.BOOL
    _invalid_handle = ctypes.c_void_p(-1).value

    class _FileAttributes(ctypes.Structure):
        _fields_ = [("attributes", wintypes.DWORD), ("tag", wintypes.DWORD)]

    def _windows_handle(path, directory):
        # Open the reparse point itself and omit FILE_SHARE_DELETE so a
        # checked directory cannot be renamed while writes use its pathname.
        flags = 0x00200000 | (0x02000000 if directory else 0)
        access = 0 if directory else 0x80000000
        value = str(path)
        if not value.startswith("\\\\?\\"):
            value = ("\\\\?\\UNC\\" + value[2:]) if value.startswith("\\\\") else "\\\\?\\" + value
        handle = _create_file(value, access, 3, None, 3, flags, None)
        if handle == _invalid_handle:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            attributes = _FileAttributes()
            if not _handle_info(handle, 9, ctypes.byref(attributes), ctypes.sizeof(attributes)):
                raise ctypes.WinError(ctypes.get_last_error())
            if attributes.attributes & 0x400 or bool(attributes.attributes & 0x10) != directory:
                raise OptimisationError(f"Refusing an unsafe destination entry: {path}.")
            return handle
        except BaseException:
            _close_handle(handle)
            raise


class _Folder:
    def __init__(self, path, fd=None):
        self.path = path
        self.fd = fd

    def argument(self, name):
        return name if self.fd is not None else self.path / name

    def stat(self, name):
        return os.stat(self.argument(name), dir_fd=self.fd, follow_symlinks=False)

    def identity(self):
        entry = os.fstat(self.fd) if self.fd is not None else self.path.stat(follow_symlinks=False)
        return entry.st_dev, entry.st_ino

    def mkdir(self, name):
        os.mkdir(self.argument(name), dir_fd=self.fd)

    def unlink(self, name):
        os.unlink(self.argument(name), dir_fd=self.fd)

    def open_read(self, name):
        if os.name == "nt":
            handle = _windows_handle(self.path / name, directory=False)
            try:
                fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
            except BaseException:
                _close_handle(handle)
                raise
        else:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=self.fd)
        stream = os.fdopen(fd, "rb")
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            stream.close()
            raise OptimisationError(f"Refusing a non-file destination: {self.path / name}.")
        return stream

    def open_stage(self, name):
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(self.argument(name), flags, 0o600, dir_fd=self.fd)
        return os.fdopen(fd, "wb")

    def publish(self, old, new, replace):
        if os.name == "nt":
            operation = os.replace if replace else os.rename
            operation(self.path / old, self.path / new)
        elif replace:
            os.replace(old, new, src_dir_fd=self.fd, dst_dir_fd=self.fd)
        else:
            _publish_new_posix(self.fd, old, new)


def _publish_new_posix(directory, old, new):
    # POSIX rename would clobber the destination. Linux and macOS provide
    # exclusive rename even on filesystems that lack hard links (like FAT).
    import ctypes

    library = ctypes.CDLL(None, use_errno=True)
    if sys.platform.startswith("linux"):
        rename = getattr(library, "renameat2", None)
        flags = 1  # RENAME_NOREPLACE
    elif sys.platform == "darwin":
        rename = getattr(library, "renameatx_np", None)
        flags = 4  # RENAME_EXCL
    else:
        rename = None
    if rename is not None:
        rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        rename.restype = ctypes.c_int
        if rename(directory, os.fsencode(old), directory, os.fsencode(new), flags) == 0:
            return
        code = ctypes.get_errno()
        if code not in (errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP):
            raise OSError(code, os.strerror(code), new)
    os.link(old, new, src_dir_fd=directory, dst_dir_fd=directory, follow_symlinks=False)


def _open_directory(stack, path, parent=None, create=False):
    if parent is not None and create:
        with suppress(FileExistsError):
            parent.mkdir(path.name)
    if os.name == "nt":
        stack.callback(_close_handle, _windows_handle(path, directory=True))
        folder = _Folder(path)
    else:
        fd = os.open(path.name if parent else path, os.O_DIRECTORY | os.O_NOFOLLOW | os.O_RDONLY,
                     dir_fd=parent.fd if parent else None)
        stack.callback(os.close, fd)
        folder = _Folder(path, fd)
    try:
        folder.stat("metadata.db")
    except FileNotFoundError:
        return folder
    raise OptimisationError(f"Choose a folder outside Calibre libraries: {path}.")


@contextmanager
def _open_chain(path, subfolders=(), expected=None, create=False):
    with ExitStack() as stack:
        position = Path(path.anchor)
        root = _open_directory(stack, position)
        for part in path.parts[1:]:
            position /= part
            root = _open_directory(stack, position, root)
        if expected is not None and root.identity() != expected:
            raise OptimisationError(f"The destination has changed or disconnected: {path}.")
        folder = root
        for part in subfolders:
            position /= part
            folder = _open_directory(stack, position, folder, create=create)
        yield root, folder


class Destination:
    """Pin an existing, user-confirmed folder before exporting any files."""

    def __init__(self, path):
        self.path = Path(path).expanduser()
        if not self.path.is_absolute() or ".." in self.path.parts or (
            os.name == "nt" and str(self.path).startswith(("\\\\.\\", "\\\\?\\GLOBALROOT"))
        ):
            raise OptimisationError("Choose an existing absolute destination folder.")
        with _open_chain(self.path) as (root, _):
            self._identity = root.identity()
        self._claimed = set()

    def export(self, source, book, options=TransferOptions(), cancel=None):
        source = Path(source)
        subfolders, stem = relative_location(book, options)
        check_cancel(cancel)
        with _open_chain(self.path, subfolders, self._identity, create=True) as (_, folder):
            for number in range(1, MAX_ATTEMPTS + 1):
                check_cancel(cancel)
                filename = f"{stem}.epub" if number == 1 else f"{stem} ({number}).epub"
                key = path_key("/".join((*subfolders, filename)))
                path = folder.path / filename
                try:
                    existing = folder.stat(filename)
                except FileNotFoundError:
                    existing = None
                if existing is not None:
                    if not stat.S_ISREG(existing.st_mode) or getattr(existing, "st_file_attributes", 0) & 0x400:
                        raise OptimisationError(f"Refusing a non-file or link at {path}.")
                    if _matches(source, folder, filename, existing.st_size, cancel):
                        self._claimed.add(key)
                        return TransferResult(path, "current")
                    if options.overwrite == "skip":
                        return TransferResult(path, "skipped")
                    if options.overwrite == "keep_both" or key in self._claimed:
                        continue
                elif key in self._claimed:
                    continue
                try:
                    self._write(source, folder, subfolders, filename, existing is not None, cancel)
                except FileExistsError:
                    if options.overwrite == "skip":
                        return TransferResult(path, "skipped")
                    continue
                self._claimed.add(key)
                return TransferResult(path, "written")
        raise OptimisationError(f"No unused filename for {stem!r}.")

    def _write(self, source, folder, subfolders, filename, replace, cancel):
        temporary = f".xteink-{uuid4().hex}.part"
        stage = folder.open_stage(temporary)
        try:
            with stage, source.open("rb") as stream:
                stream_copy(stream, stage, cancel)
                stage.flush()
                os.fsync(stage.fileno())
            check_cancel(cancel)
            with _open_chain(self.path, subfolders, self._identity) as (_, confirmed):
                if confirmed.identity() != folder.identity():
                    raise OptimisationError(f"The export folder changed during copying: {folder.path}.")
                check_cancel(cancel)
                folder.publish(temporary, filename, replace)
        finally:
            # If the drive disappeared, there can be an inert .part file
            # on the card. Cleanup must not hit a new mount at the same letter.
            if folder.fd is not None:
                with suppress(OSError):
                    folder.unlink(temporary)
            else:
                with suppress(OSError, OptimisationError):
                    with _open_chain(self.path, subfolders, self._identity) as (_, confirmed):
                        if confirmed.identity() == folder.identity():
                            folder.unlink(temporary)


def _matches(source, folder, name, size, cancel):
    if source.stat().st_size != size:
        return False
    with source.open("rb") as original, folder.open_read(name) as destination:
        for block in chunks(original, cancel):
            if destination.read(len(block)) != block:
                return False
        return not destination.read(1)
