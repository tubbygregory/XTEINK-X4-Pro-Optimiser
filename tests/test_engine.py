"""Portable EPUB and image tests."""

from dataclasses import replace
from io import BytesIO
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from unittest import TestCase, mock
from zipfile import ZIP_STORED, ZipFile, ZipInfo

from PIL import Image

from fixtures import CSS, SVG, image_bytes, make_epub
from xteink.engine import optimise_epub
from xteink.io_utils import Cancelled, OptimisationError
from xteink.settings import Options, PRESETS
from xteink.validation import find_epubcheck, validate_epub


class EngineTests(TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        self.source = self.folder / "master.epub"
        self.output = self.folder / "output.epub"

    def test_epub2_and_epub3_text_are_identical(self):
        for version in (2, 3):
            with self.subTest(version=version):
                make_epub(self.source, version=version)
                original = self.source.read_bytes()
                result = optimise_epub(self.source, self.output)
                self.assertEqual(result.changed_images, 0)
                self.assertEqual(self.source.read_bytes(), original)
                self.assertEqual(self.output.read_bytes(), original)
                validate_epub(self.output)

    def test_master_book_and_international_markup_are_preserved(self):
        images = {
            "画像/表紙.jpg": ("image/jpeg", image_bytes(size=(900, 1200), quality=98), True),
            "opaque.png": ("image/png", image_bytes("PNG"), True),
            "transparent.png": ("image/png", image_bytes("PNG", "RGBA"), True),
            "vector.svg": ("image/svg+xml", SVG, True),
            "gray.jpg": ("image/jpeg", image_bytes("JPEG", "L", quality=65), True),
        }
        make_epub(
            self.source, images=images, cover="画像/表紙.jpg",
            css=CSS + b"@font-face { font-family: Custom; src: url('fonts/test.ttf'); }",
            font=True, math=True,
        )
        original = self.source.read_bytes()
        result = optimise_epub(self.source, self.output)
        publication = validate_epub(self.output)
        self.assertEqual(self.source.read_bytes(), original)
        self.assertEqual(result.changed_images, 3)
        self.assertIn("EPUB/画像/表紙.jpg", publication.covers)
        with ZipFile(self.source) as before, ZipFile(self.output) as after:
            self.assertEqual(set(after.namelist()), set(before.namelist()))
            self.assertEqual(after.infolist()[0].filename, "mimetype")
            self.assertEqual(after.infolist()[0].compress_type, ZIP_STORED)
            self.assertEqual(after.infolist()[0].extra, b"")
            for name in before.namelist():
                if name not in {"EPUB/画像/表紙.jpg", "EPUB/opaque.png", "EPUB/transparent.png"}:
                    self.assertEqual(before.read(name), after.read(name), name)
            with Image.open(BytesIO(after.read("EPUB/画像/表紙.jpg"))) as image:
                self.assertEqual((image.mode, image.size), ("L", (480, 640)))
            with Image.open(BytesIO(after.read("EPUB/transparent.png"))) as image:
                self.assertEqual(image.size, (64, 40))
                self.assertEqual(image.mode, "LA")
                self.assertEqual(image.getpixel((0, 0))[1], 0)
                self.assertEqual(image.getpixel((4, 4))[1], 120)
            self.assertIsNone(after.testzip())

    def test_cover_exclusion_and_maximum_quality(self):
        data = image_bytes(size=(600, 900), quality=99)
        make_epub(self.source, version=2, images={"cover.jpg": ("image/jpeg", data, True)}, cover="cover.jpg")
        for options in (replace(Options(), optimise_cover=False), PRESETS["Maximum Quality"]):
            result = optimise_epub(self.source, self.output, options)
            self.assertFalse(result.changed_images)
            self.assertEqual(self.output.read_bytes(), self.source.read_bytes())

    def test_exif_rotation_then_resize(self):
        exif = Image.Exif()
        exif[274] = 6
        data = image_bytes(size=(600, 300), exif=exif, quality=97)
        make_epub(self.source, images={"photo.jpg": ("image/jpeg", data, True)})
        optimise_epub(self.source, self.output, replace(Options(), max_width=240, max_height=480))
        with ZipFile(self.output) as archive, Image.open(BytesIO(archive.read("EPUB/photo.jpg"))) as image:
            self.assertEqual(image.size, (240, 480))
            self.assertNotIn(274, image.getexif())

    def test_dithers_and_optional_white_background(self):
        make_epub(self.source, images={"alpha.png": ("image/png", image_bytes("PNG", "RGBA"), True)})
        for method in ("floyd-steinberg", "ordered"):
            with self.subTest(method=method):
                options = replace(Options(), dither=method, preserve_transparency=False)
                optimise_epub(self.source, self.output, options)
                with ZipFile(self.output) as archive, Image.open(BytesIO(archive.read("EPUB/alpha.png"))) as image:
                    self.assertEqual(image.mode, "L")
                    self.assertEqual(image.getpixel((0, 0)), 255)
                    self.assertEqual(set(image.tobytes()), {0, 255})

    def test_static_gif_transparency_and_svg_comment_cleanup(self):
        image = Image.new("P", (32, 24))
        image.putpalette([255, 0, 0, 0, 255, 0] + [0] * 762)
        image.paste(1, (5, 5, 25, 18))
        stream = BytesIO()
        image.save(stream, "GIF", transparency=0)
        make_epub(self.source, images={
            "figure.gif": ("image/gif", stream.getvalue(), True),
            "logo.svg": ("image/svg+xml", SVG, True),
        })
        optimise_epub(self.source, self.output, replace(Options(), preserve_svg=False))
        with ZipFile(self.output) as archive, Image.open(BytesIO(archive.read("EPUB/figure.gif"))) as new:
            self.assertEqual(new.convert("RGBA").getpixel((0, 0))[3], 0)
            self.assertEqual(new.convert("RGBA").getpixel((8, 8))[3], 255)
            self.assertNotIn(b"<!--", archive.read("EPUB/logo.svg"))
            self.assertIn(b"<rect", archive.read("EPUB/logo.svg"))

    def test_animation_and_unsupported_images_are_retained(self):
        stream = BytesIO()
        frames = [Image.new("RGB", (20, 20), colour) for colour in ("red", "blue")]
        frames[0].save(stream, "GIF", append_images=frames[1:], save_all=True, loop=0)
        make_epub(self.source, images={
            "animation.gif": ("image/gif", stream.getvalue(), True),
            "modern.webp": ("image/webp", b"opaque unsupported image data", False),
        })
        result = optimise_epub(self.source, self.output)
        self.assertEqual(result.changed_images, 0)
        self.assertEqual(len(result.warnings), 2)
        self.assertEqual(self.source.read_bytes(), self.output.read_bytes())

    def test_jpeg_quality_only_and_already_optimised(self):
        make_epub(self.source, images={"photo.jpg": ("image/jpeg", image_bytes(quality=100), True)})
        options = replace(Options(), grayscale=False)
        result = optimise_epub(self.source, self.output, options)
        self.assertEqual(result.changed_images, 1)
        second = self.folder / "second.epub"
        self.assertEqual(optimise_epub(self.output, second, options).changed_images, 0)
        self.assertEqual(self.output.read_bytes(), second.read_bytes())

    def test_compatibility_fix_and_strict_container(self):
        make_epub(self.source, stored_first=False)
        with self.assertRaisesRegex(OptimisationError, "mimetype entry"):
            optimise_epub(self.source, self.output, replace(Options(), compatibility_fixes=False))
        self.assertTrue(optimise_epub(self.source, self.output).changed_structure)
        validate_epub(self.output)

    def test_bad_epub_leaves_an_existing_output_intact(self):
        self.source.write_bytes(b"bad zip")
        self.output.write_bytes(b"previous export")
        with self.assertRaises(OptimisationError):
            optimise_epub(self.source, self.output)
        self.assertEqual(self.output.read_bytes(), b"previous export")
        self.assertEqual(self.source.read_bytes(), b"bad zip")

    def test_rejects_malformed_document_and_bad_navigation(self):
        make_epub(self.source, malformed="EPUB/text/chapter.xhtml")
        with self.assertRaisesRegex(OptimisationError, "Malformed XML"):
            optimise_epub(self.source, self.output)
        make_epub(self.source, nav_fragment="does-not-exist")
        with self.assertRaisesRegex(OptimisationError, "Missing navigation anchor"):
            optimise_epub(self.source, self.output)

    def test_rejects_missing_resources_and_corrupt_raster(self):
        make_epub(self.source, images={"bad.jpg": ("image/jpeg", b"not an image", True)})
        with self.assertRaisesRegex(OptimisationError, "Invalid JPEG"):
            optimise_epub(self.source, self.output)
        make_epub(self.source, extra={"EPUB/text/chapter.xhtml": b'<html xmlns="http://www.w3.org/1999/xhtml"><body><img src="missing.jpg"/></body></html>'})
        with self.assertRaisesRegex(OptimisationError, "Missing EPUB resource"):
            optimise_epub(self.source, self.output)

    def test_rejects_archive_paths_and_drm(self):
        for name, phrase in (
            ("../escapes", "Unsafe EPUB entry"), ("AUX.txt", "Unsafe EPUB entry"),
            ("C:drive", "Unsafe EPUB entry"), ("a//b", "Unsafe EPUB entry"),
            ("epub/本.OPF", "Duplicate or ambiguous"),
            ("META-INF/encryption.xml", "encryption"),
            ("META-INF/rights.xml", "DRM"),
        ):
            with self.subTest(name=name):
                make_epub(self.source, extra={name: b"x"})
                with self.assertRaisesRegex(OptimisationError, phrase):
                    optimise_epub(self.source, self.output)

    def test_rejects_symlink_and_zip_expansion(self):
        make_epub(self.source)
        with ZipFile(self.source, "a") as archive:
            link = ZipInfo("link")
            link.create_system = 3
            link.external_attr = 0o120777 << 16
            archive.writestr(link, "../../outside")
        with self.assertRaisesRegex(OptimisationError, "Unsupported special EPUB entry"):
            optimise_epub(self.source, self.output)
        make_epub(self.source, extra={"huge": bytes(10_000)})
        with mock.patch("xteink.archive.MAX_MEMBER", 8_000):
            with self.assertRaisesRegex(OptimisationError, "EPUB expansion limit"):
                optimise_epub(self.source, self.output)

    def test_rejects_excessive_zip_compression_and_central_directory_size(self):
        make_epub(self.source, extra={"too-compressed": bytes(3 * 1024 * 1024)})
        with self.assertRaisesRegex(OptimisationError, "EPUB expansion limit"):
            optimise_epub(self.source, self.output)
        make_epub(self.source)
        with mock.patch("xteink.archive.MAX_DIRECTORY", 100):
            with self.assertRaisesRegex(OptimisationError, "central directory exceeds"):
                optimise_epub(self.source, self.output)

    def test_rejects_unsafe_package_uri_and_xml_entities(self):
        from fixtures import CONTAINER
        make_epub(self.source, extra={
            "META-INF/container.xml": CONTAINER.replace("EPUB/本.opf", "%2e%2e/%2e%2e/file"),
        })
        with self.assertRaisesRegex(OptimisationError, "outside the container"):
            optimise_epub(self.source, self.output)
        make_epub(self.source, extra={
            "EPUB/text/chapter.xhtml": b'<!DOCTYPE html [<!ENTITY injected SYSTEM "file:///etc/passwd">]><html>&injected;</html>',
        })
        with self.assertRaisesRegex(OptimisationError, "Custom DTD"):
            optimise_epub(self.source, self.output)

    def test_image_pixel_limit_and_cancellation_inside_a_book(self):
        make_epub(self.source, images={"photo.jpg": ("image/jpeg", image_bytes(size=(200, 200)), True)})
        with mock.patch("xteink.images.MAX_PIXELS", 1_000):
            with self.assertRaisesRegex(OptimisationError, "Image pixel limit"):
                optimise_epub(self.source, self.output)
        event = Event()
        def stop(message):
            if "image" in message:
                event.set()
        with self.assertRaises(Cancelled):
            optimise_epub(self.source, self.output, cancel=event, progress=stop)
        self.assertFalse(self.output.exists())
        self.assertFalse(list(self.folder.glob(".xteink-*.part")))

    def test_same_path_never_overwritten(self):
        make_epub(self.source)
        with self.assertRaisesRegex(OptimisationError, "separate from"):
            optimise_epub(self.source, self.source)

    def test_epubcheck_when_available(self):
        command = find_epubcheck(os.environ.get("XTEINK_TEST_EPUBCHECK", ""))
        if not command:
            self.skipTest("EPUBCheck not installed")
        for version in (2, 3):
            with self.subTest(version=version):
                make_epub(self.source, version=version, images={
                    "cover.jpg": ("image/jpeg", image_bytes(size=(500, 800)), True),
                    "alpha.png": ("image/png", image_bytes("PNG", "RGBA"), True),
                }, cover="cover.jpg")
                optimise_epub(self.source, self.output, replace(Options(), validate_epubcheck=True), epubcheck=command)
