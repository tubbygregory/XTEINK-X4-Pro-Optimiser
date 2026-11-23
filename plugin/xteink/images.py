"""Optimise raster images in their existing format and never rasterise SVG."""

from functools import lru_cache
from io import BytesIO
import re

from lxml import etree
from PIL import Image, ImageOps, UnidentifiedImageError

from .io_utils import OptimisationError, check_cancel
from .validation import xml_tree


FORMATS = {"image/jpeg": "JPEG", "image/png": "PNG", "image/gif": "GIF"}
MAX_PIXELS = 32_000_000
BAYER = (
    (0, 8, 2, 10), (12, 4, 14, 6), (3, 11, 1, 9), (15, 7, 13, 5),
)
ORDERED_TABLES = tuple(
    tuple(bytes(255 if level >= cell * 16 + 8 else 0 for level in range(256)) for cell in row)
    for row in BAYER
)


@lru_cache(maxsize=12)
def jpeg_tables(quality):
    with BytesIO() as stream:
        Image.new("RGB", (8, 8)).save(stream, "JPEG", quality=quality)
        stream.seek(0)
        with Image.open(stream, formats=["JPEG"]) as jpeg:
            return jpeg.quantization


def finer_jpeg_tables(image, quality):
    reference = jpeg_tables(quality)
    present = image.quantization or {}
    if not present or not present.keys() <= reference.keys():
        return False
    values = [(actual, target) for key, table in present.items() for actual, target in zip(table, reference[key])]
    return all(actual <= target for actual, target in values) and any(actual < target for actual, target in values)


def bilevel(gray, method, cancel):
    if method == "floyd-steinberg":
        return gray.convert("1", dither=Image.Dither.FLOYDSTEINBERG).convert("L")
    width, height = gray.size
    raw = gray.tobytes()
    output = bytearray(len(raw))
    for y in range(height):
        if y % 64 == 0:
            check_cancel(cancel)
        start, end = y * width, (y + 1) * width
        for x, table in enumerate(ORDERED_TABLES[y % 4]):
            output[start + x:end:4] = raw[start + x:end:4].translate(table)
    return Image.frombytes("L", gray.size, bytes(output))


def optimise_svg(data, name):
    if b"<!--" not in data:
        return None
    root = xml_tree(data, name)
    tree = root.getroottree()
    if tree.docinfo.doctype or root.xpath(
        '//*[local-name()="script"]'
    ) or any(
        re.match(r"on[a-z]", attr, re.IGNORECASE) for node in root.iter(etree.Element) for attr in node.attrib
    ):
        return None
    for comment in root.xpath(".//comment()"):
        parent = comment.getparent()
        if comment.tail:
            previous = comment.getprevious()
            if previous is None:
                parent.text = (parent.text or "") + comment.tail
            else:
                previous.tail = (previous.tail or "") + comment.tail
        parent.remove(comment)
    result = etree.tostring(tree, encoding="UTF-8", xml_declaration=True)
    return result if len(result) < len(data) else None


def optimise_image(data, name, media_type, options, cancel=None):
    """Return replacement bytes, if needed, and a user-facing warning."""
    if media_type == "image/svg+xml":
        replacement = None if options.preserve_svg else optimise_svg(data, name)
        return replacement, ""
    image_format = FORMATS.get(media_type)
    if image_format is None:
        return None, f"Retained {name}: image type {media_type} is not optimised."
    try:
        with Image.open(BytesIO(data), formats=[image_format]) as image:
            if image.width * image.height > MAX_PIXELS:
                raise OptimisationError(f"Image pixel limit exceeded in {name!r}.")
            if getattr(image, "is_animated", False):
                # Keep frame disposal, timing, palette and transparency intact.
                return None, f"Retained animated image {name}."
            orientation = image.getexif().get(274, 1)
            correct_orientation = orientation in range(2, 9)
            width, height = image.size[::-1] if orientation in (5, 6, 7, 8) else image.size
            resize = options.resize and (width > options.max_width or height > options.max_height)
            gray = image.mode in ("1", "L", "LA")
            if image.mode == "P":
                palette = image.getpalette("RGB") or []
                gray = all(r == g == b for r, g, b in zip(palette[::3], palette[1::3], palette[2::3]))
            alpha = "A" in image.getbands() or "transparency" in image.info
            flatten = alpha and not options.preserve_transparency
            grayscale = (options.grayscale or options.dither != "off") and not gray
            dither = options.dither != "off" and image.mode != "1"
            progressive = image_format == "JPEG" and options.compatibility_fixes and bool(image.info.get("progressive"))
            quality = image_format == "JPEG" and finer_jpeg_tables(image, options.jpeg_quality)

            if dither and gray and not resize and image.mode in ("L", "LA"):
                tones = image.getchannel("L").getcolors(2)
                dither = tones is None or any(tone not in (0, 255) for _, tone in tones)
            required = resize or grayscale or flatten or dither or correct_orientation or progressive
            if not required and not quality:
                # Decode once to catch corrupt inputs even if already at target.
                image.load()
                return None, ""
            icc = image.info.get("icc_profile")
            original_mode = image.mode
            image = ImageOps.exif_transpose(image) if correct_orientation else image.copy()
            if image.mode.startswith("I;16") and (options.grayscale or dither):
                image = image.convert("I").point(lambda value: value * (1 / 257)).convert("L")

            if alpha:
                image = image.convert("RGBA")
                if flatten:
                    matte = Image.new("RGB", image.size, "white")
                    matte.paste(image, mask=image.getchannel("A"))
                    image = matte
            elif image.mode == "P":
                image = image.convert("L" if gray else "RGB")
            if resize:
                image.thumbnail((options.max_width, options.max_height), Image.Resampling.LANCZOS)
            if options.grayscale or dither:
                mask = image.getchannel("A") if "A" in image.getbands() else None
                image = image.convert("L")
                if dither:
                    image = bilevel(image, options.dither, cancel)
                if mask is not None:
                    image.putalpha(mask)

            check_cancel(cancel)
            image.info.clear()
            params = {"icc_profile": icc} if icc and image.mode == original_mode and image_format != "GIF" else {}
            if image_format == "JPEG":
                if image.mode not in ("L", "RGB", "CMYK"):
                    image = image.convert("RGB")
                params.update(quality=options.jpeg_quality, optimize=True, progressive=False)
            elif image_format == "PNG":
                params["optimize"] = True
            elif "A" in image.getbands():
                alpha_mask = image.getchannel("A").point(lambda value: 255 if value < 128 else 0)
                image = image.convert("RGB").quantize(colors=255)
                image.paste(255, mask=alpha_mask)
                params["transparency"] = 255
                params["optimize"] = False
            with BytesIO() as output:
                image.save(output, image_format, **params)
                result = output.getvalue()
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as error:
        raise OptimisationError(f"Image pixel limit exceeded in {name!r}.") from error
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError) as error:
        raise OptimisationError(f"Invalid {image_format} image {name!r}: {error}") from error
    return (result if required or len(result) < len(data) else None), ""
