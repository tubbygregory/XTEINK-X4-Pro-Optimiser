"""Run a sequence of staged books independently with cancellation and reporting."""

from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
import traceback

from .archive import MAX_ARCHIVE
from .engine import optimise_epub
from .io_utils import Cancelled, OptimisationError, check_cancel


class SourceWriter:
    """File-like sink for Calibre's read-only copy_format_to API."""

    def __init__(self, stream, cancel):
        self.stream = stream
        self.cancel = cancel
        self.size = 0
        self.digest = sha256()

    def write(self, data):
        check_cancel(self.cancel)
        self.size += len(data)
        if self.size > MAX_ARCHIVE:
            raise OptimisationError("The EPUB exceeds the compressed archive size limit.")
        self.digest.update(data)
        return self.stream.write(data)

    def flush(self):
        self.stream.flush()


@dataclass
class BatchSummary:
    total: int
    written: int = 0
    current: int = 0
    skipped_existing: int = 0
    missing: int = 0
    failed: int = 0
    cancelled: bool = False
    cache_hits: int = 0
    scanned_images: int = 0
    changed_images: int = 0
    changed_structure: int = 0
    original_bytes: int = 0
    output_bytes: int = 0
    details: list[str] = field(default_factory=list)
    omitted_details: int = 0

    def detail(self, line):
        if len(self.details) < 80:
            self.details.append(line)
        else:
            self.omitted_details += 1


def process_batch(
    book_ids, get_book, copy_epub, destination, transfer, options, *,
    cache=None, scratch=None, epubcheck=(), structure_editor=None,
    cancel=None, progress=None, log=None,
):
    """Callbacks supply metadata and copy raw EPUB bytes into a file-like sink."""
    result = BatchSummary(total=len(book_ids))
    if progress is None:
        def progress(index, message):
            pass
    if log is None:
        def log(message):
            pass
    for index, book_id in enumerate(book_ids):
        label = f"Book {book_id}"
        try:
            check_cancel(cancel)
            book = get_book(book_id)
            label = book.title
            progress(index, f"{label} · copying EPUB from the library")
            with TemporaryDirectory(prefix="xteink-book-", dir=scratch) as directory:
                source = Path(directory) / "original.epub"
                with source.open("wb") as file:
                    writer = SourceWriter(file, cancel)
                    available = copy_epub(book_id, writer)
                if not available:
                    result.missing += 1
                    result.detail(f"{label}: no EPUB format")
                    log(f"{book_id} {label!r}: no EPUB format")
                    continue

                stats = None
                product = source
                if options is not None:
                    def stage(message):
                        progress(index, f"{label} · {message}")
                    kwargs = dict(cancel=cancel, progress=stage, scratch=directory,
                                  epubcheck=epubcheck, structure_editor=structure_editor)
                    if cache is None:
                        product = Path(directory) / "optimised.epub"
                        stats = optimise_epub(source, product, options, **kwargs)
                    else:
                        product, stats = cache.get_or_optimise(source, writer.digest.hexdigest(), options, **kwargs)
                progress(index, f"{label} · exporting")
                exported = destination.export(product, book, transfer, cancel)
                if exported.status == "written":
                    result.written += 1
                elif exported.status == "current":
                    result.current += 1
                else:
                    result.skipped_existing += 1
                if stats:
                    result.cache_hits += int(stats.cache_hit)
                    result.scanned_images += stats.scanned_images
                    result.changed_images += stats.changed_images
                    result.changed_structure += int(stats.changed_structure)
                    result.original_bytes += stats.original_bytes
                    result.output_bytes += stats.output_bytes
                    for warning in stats.warnings:
                        result.detail(f"{label}: {warning}")
                    result.omitted_details += stats.omitted_warnings
                    if stats.warnings:
                        log("\n".join(stats.warnings))
                log(f"{book_id} {label!r}: {exported.status} {exported.path}"
                    + (f" | {stats.original_bytes} → {stats.output_bytes} bytes, "
                       f"{stats.changed_images} images, cache={stats.cache_hit}" if stats else ""))
        except Cancelled:
            result.cancelled = True
            break
        except Exception as error:
            result.failed += 1
            result.detail(f"{label}: {error}")
            log(f"{book_id} {label!r}: {traceback.format_exc()}")
    return result
