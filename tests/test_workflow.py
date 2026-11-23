"""Cache, batch and safe transfer regression tests."""

from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from unittest import TestCase, mock

from fixtures import image_bytes, make_epub
from xteink.batch import process_batch
from xteink.cache import Cache, cache_key
from xteink.io_utils import Cancelled, OptimisationError, hash_file, stream_copy
from xteink.settings import Options, export_presets, import_presets
from xteink.transfer import Book, Destination, TransferOptions, relative_location
from xteink.validation import validate_epub


class WorkflowTests(TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.export = self.root / "export"
        self.export.mkdir()
        self.destination = Destination(self.export)
        self.cache = Cache(self.root / "cache")
        self.source = make_epub(self.root / "source.epub", images={
            "photo.jpg": ("image/jpeg", image_bytes(size=(600, 900)), True),
        })
        self.good = Book(1, "旅と海", "Amélie García")
        self.flat = TransferOptions(structure="flat", template="{title}")

    def test_cache_hit_and_invalidation(self):
        initial = hash_file(self.source)
        options = Options()
        cached, stats = self.cache.get_or_optimise(self.source, initial, options)
        self.assertFalse(stats.cache_hit)
        self.assertEqual(self.cache.get_or_optimise(self.source, initial, options)[0], cached)
        self.assertTrue(self.cache.get_or_optimise(self.source, initial, options)[1].cache_hit)
        different = replace(options, jpeg_quality=55)
        edited, _ = self.cache.get_or_optimise(self.source, initial, different)
        self.assertNotEqual(edited, cached)
        self.assertNotEqual(cache_key(initial, options), cache_key(hash_file(edited), options))
        with mock.patch("xteink.cache.ENGINE_VERSION", "100.0.0"):
            self.assertNotEqual(cache_key(initial, options), cached.stem)
        fake = self.cache.directory / "keep.txt"
        fake.write_text("unrelated item")
        self.assertEqual(self.cache.clear()[0], 2)
        self.assertTrue(fake.is_file())
        self.assertEqual(hash_file(self.source), initial)

    def test_corrupt_cache_entry_is_rebuilt(self):
        checksum = hash_file(self.source)
        path, _ = self.cache.get_or_optimise(self.source, checksum, Options())
        path.write_bytes(b"interrupted disk operation")
        rebuilt, stats = self.cache.get_or_optimise(self.source, checksum, Options())
        self.assertEqual(path, rebuilt)
        self.assertFalse(stats.cache_hit)
        validate_epub(rebuilt)

    def test_filename_template_unicode_and_reserved_names(self):
        book = Book(15, r"../旅:海\三", "Café", "NUL")
        folders, stem = relative_location(book, TransferOptions("{title} {id}.epub", "series"))
        self.assertEqual(folders, ("_NUL",))
        self.assertNotIn("/", stem)
        self.assertNotIn("\\", stem)
        self.assertIn("旅", stem)
        self.assertTrue(stem.endswith("15"))
        for template in ("{title.__class__}", "{authors[0]}", "{id!r}", "{title:>40}"):
            with self.assertRaises(ValueError):
                TransferOptions(template)
        default_loc, _ = relative_location(Book(1, "Title", "Various people", first_author="Primary"), TransferOptions())
        self.assertEqual(default_loc, ("Primary",))

    def test_keep_both_current_skip_and_replace(self):
        first = self.destination.export(self.source, self.good, self.flat)
        self.assertEqual(first.status, "written")
        self.assertEqual(first.path.name, "旅と海.epub")
        again = self.destination.export(self.source, self.good, self.flat)
        self.assertEqual(again.status, "current")
        second_source = make_epub(self.root / "other.epub")
        keep = self.destination.export(second_source, self.good, self.flat)
        self.assertEqual(keep.path.name, "旅と海 (2).epub")
        self.assertEqual(Destination(self.export).export(second_source, self.good, self.flat).path, keep.path)
        skipped = Destination(self.export).export(second_source, self.good, replace(self.flat, overwrite="skip"))
        self.assertEqual(skipped.status, "skipped")
        replaced = Destination(self.export).export(second_source, self.good, replace(self.flat, overwrite="replace"))
        self.assertEqual(replaced.path, first.path)
        self.assertEqual(first.path.read_bytes(), second_source.read_bytes())

    def test_replace_policy_keeps_different_books_with_same_name_in_a_batch(self):
        options = replace(self.flat, overwrite="replace")
        first = self.destination.export(self.source, self.good, options)
        second_source = make_epub(self.root / "other.epub")
        second = self.destination.export(second_source, replace(self.good, id=2), options)
        self.assertNotEqual(first.path, second.path)
        self.assertEqual(first.path.read_bytes(), self.source.read_bytes())

    def test_destination_must_be_outside_any_calibre_library(self):
        library = self.root / "library"
        deeper = library / "Author"
        deeper.mkdir(parents=True)
        (library / "metadata.db").touch()
        with self.assertRaisesRegex(OptimisationError, "outside Calibre libraries"):
            Destination(deeper)
        (self.export / "Amélie García").mkdir()
        (self.export / "Amélie García" / "metadata.db").touch()
        with self.assertRaisesRegex(OptimisationError, "outside Calibre libraries"):
            self.destination.export(self.source, self.good)

    def test_symlinked_directory_and_file_are_rejected(self):
        target = self.root / "outside"
        target.mkdir()
        try:
            (self.export / "Amélie García").symlink_to(target, target_is_directory=True)
            (self.export / "旅と海.epub").symlink_to(self.source)
        except OSError:
            self.skipTest("OS does not permit symlinks for this test account")
        original = self.source.read_bytes()
        with self.assertRaises((OSError, OptimisationError)):
            self.destination.export(self.source, self.good)
        with self.assertRaisesRegex(OptimisationError, "non-file or link"):
            self.destination.export(self.source, self.good, replace(self.flat, overwrite="replace"))
        self.assertEqual(self.source.read_bytes(), original)
        self.assertFalse(list(target.iterdir()))

    def test_destination_change_is_detected_before_creating_subfolder(self):
        self.export.rename(self.root / "previous")
        self.export.mkdir()
        with self.assertRaisesRegex(OptimisationError, "changed or disconnected"):
            self.destination.export(self.source, self.good)
        self.assertFalse(list(self.export.iterdir()))

    def test_interrupted_transfer_does_not_replace_existing_file(self):
        first = self.destination.export(self.source, self.good, self.flat)
        original = first.path.read_bytes()
        new = make_epub(self.root / "new.epub")
        fresh = Destination(self.export)
        with mock.patch("xteink.transfer.os.fsync", side_effect=OSError("device disconnected")):
            with self.assertRaises(OSError):
                fresh.export(new, self.good, replace(self.flat, overwrite="replace"))
        self.assertEqual(first.path.read_bytes(), original)
        self.assertEqual(list(self.export.iterdir()), [first.path])

    def test_transfer_cancellation_cleans_partial_copy(self):
        cancel = Event()
        def stop_after_copy(source, target, cancel):
            target.write(source.read(20))
            cancel.set()
        with mock.patch("xteink.transfer.stream_copy", side_effect=stop_after_copy):
            with self.assertRaises(Cancelled):
                self.destination.export(self.source, self.good, self.flat, cancel)
        self.assertFalse(list(self.export.iterdir()))

    def test_batch_continues_after_failures_and_keeps_masters_unchanged(self):
        bad = self.root / "bad.epub"
        bad.write_bytes(b"invalid EPUB")
        book_files = {1: bad, 2: self.source}

        def read_book(book_id):
            return Book(book_id, f"Title {book_id}", "Author")

        def copy(book_id, writer):
            if book_id not in book_files:
                return False
            with book_files[book_id].open("rb") as source:
                stream_copy(source, writer)
            return True

        before = self.source.read_bytes()
        results = process_batch((1, 2, 3), read_book, copy, self.destination, self.flat, Options(), cache=self.cache)
        self.assertEqual((results.written, results.failed, results.missing), (1, 1, 1))
        self.assertEqual(self.source.read_bytes(), before)
        self.assertEqual(bad.read_bytes(), b"invalid EPUB")
        validate_epub(self.export / "Title 2.epub")
        retry = process_batch((2,), read_book, copy, Destination(self.export), self.flat, Options(), cache=self.cache)
        self.assertEqual((retry.current, retry.cache_hits), (1, 1))

    def test_batch_cancels_between_processed_books(self):
        event = Event()
        processed = []

        def read_book(book_id):
            processed.append(book_id)
            return replace(self.good, id=book_id)

        def copy(book_id, writer):
            writer.write(self.source.read_bytes())
            return True

        def progress(index, message):
            if index == 0 and "exporting" in message:
                event.set()

        results = process_batch(
            (1, 2), read_book, copy, self.destination, self.flat, None,
            cancel=event, progress=progress,
        )
        self.assertTrue(results.cancelled)
        self.assertEqual(processed, [1])
        self.assertFalse(list(self.export.iterdir()))

    def test_profiles_round_trip_and_reject_invalid_json(self):
        options = replace(Options(), dither="ordered", jpeg_quality=60)
        self.assertEqual(import_presets(export_presets({"Mon réglage": options})), {"Mon réglage": options})
        invalid = (
            b'{"schema":1,"presets":{"Balanced":{}}}',
            b'{"schema":1,"presets":{"user":{"jpeg_quality":101}}}',
            b'{"schema":1,"presets":{"user":{"grayscale":1}}}',
            b'{"schema":1,"presets":{"user":{"foobar":0}}}',
        )
        for data in invalid:
            with self.assertRaises(ValueError):
                import_presets(data)
