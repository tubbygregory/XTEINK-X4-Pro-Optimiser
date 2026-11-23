"""Image-first EPUB optimisation with an optional Calibre structure editor."""

from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory

from .archive import MAX_MEMBER, check_contents, open_archive, write_archive
from .images import FORMATS, optimise_image
from .io_utils import OptimisationError, atomic_output, check_cancel, stream_copy
from .settings import Options
from .validation import MAX_XML, read_publication, run_epubcheck, validate_epub


@dataclass
class Statistics:
    original_bytes: int
    output_bytes: int = 0
    scanned_images: int = 0
    changed_images: int = 0
    changed_structure: bool = False
    cache_hit: bool = False
    warnings: list[str] = field(default_factory=list)
    omitted_warnings: int = 0

    def warn(self, message):
        if len(self.warnings) < 24:
            self.warnings.append(message)
        else:
            self.omitted_warnings += 1


def _images(archive, entries, publication, options, work, statistics, cancel, progress):
    replacements = {}
    if options.retain_images:
        return replacements
    for resource in publication.resources:
        check_cancel(cancel)
        if not resource.name or not resource.media_type.startswith("image/"):
            continue
        name = resource.name
        if name in publication.covers and not options.optimise_cover:
            continue
        statistics.scanned_images += 1
        if progress:
            progress(f"Checking image {statistics.scanned_images}")
        if resource.media_type == "image/svg+xml" and options.preserve_svg:
            continue
        entry = entries[name]
        # Compressed raster data is bounded separately from decoded pixel counts.
        max_size = MAX_XML if resource.media_type == "image/svg+xml" else MAX_MEMBER // 2
        if entry.file_size > max_size:
            statistics.warn(f"Retained {name}: encoded image exceeds the optimisation size limit.")
            continue
        data = archive.read(entry) if resource.media_type in FORMATS or resource.media_type == "image/svg+xml" else b""
        replacement, warning = optimise_image(data, name, resource.media_type, options, cancel)
        if warning:
            statistics.warn(warning)
        if replacement is not None:
            path = work / f"image-{statistics.scanned_images}"
            path.write_bytes(replacement)
            replacements[name] = path
            statistics.changed_images += 1
    return replacements


def optimise_epub(
    source, destination, options=Options(), *, cancel=None, progress=None,
    scratch=None, epubcheck=(), structure_editor=None,
):
    """Create a structurally checked copy; publish it atomically on success.

    An optional structure_editor implements Calibre-specific editing. Source
    must be a stable file for the duration of the call.
    """
    source, destination = Path(source), Path(destination)
    if source.resolve() == destination.resolve() or (destination.exists() and source.samefile(destination)):
        raise OptimisationError("Choose a destination separate from the original EPUB.")
    if options.needs_calibre and structure_editor is None:
        raise OptimisationError("The chosen EPUB structure options require Calibre's editing tools.")

    statistics = Statistics(source.stat().st_size)
    check_cancel(cancel)
    with TemporaryDirectory(prefix="xteink-epub-", dir=scratch) as folder:
        work = Path(folder)
        staged = source
        if progress:
            progress("Checking EPUB structure")
        with open_archive(source) as archive:
            entries, publication = read_publication(archive, normalise=options.compatibility_fixes, cancel=cancel)
            replacements = _images(archive, entries, publication, options, work, statistics, cancel, progress)
            if replacements or publication.normalise_zip:
                if progress:
                    progress("Writing EPUB")
                staged = work / "images.epub"
                write_archive(archive, entries, replacements, staged, cancel)
                statistics.changed_structure = publication.normalise_zip
            else:
                check_contents(archive, entries, cancel)

        if options.needs_calibre:
            if progress:
                progress("Checking optional EPUB edits")
            edited = work / "structured.epub"
            if structure_editor(staged, edited, work, publication, options, cancel, statistics.warn):
                check_cancel(cancel)
                staged = edited
                statistics.changed_structure = True

        if staged != source:
            validate_epub(staged, cancel)
        if options.validate_epubcheck:
            if progress:
                progress("Running EPUBCheck")
            warning = run_epubcheck(epubcheck, staged, cancel)
            if warning:
                statistics.warn(warning)

        with atomic_output(destination, cancel) as temporary:
            with staged.open("rb") as input_file, temporary.open("wb") as output_file:
                stream_copy(input_file, output_file, cancel)
        statistics.output_bytes = destination.stat().st_size
    return statistics
