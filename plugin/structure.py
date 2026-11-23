"""Opt-in structural editing with Calibre's Edit Book tools."""

from functools import partial
from pathlib import Path
import re

from calibre.ebooks.oeb.base import OEB_DOCS, OEB_STYLES
from calibre.ebooks.oeb.polish.container import get_container
from calibre.ebooks.oeb.polish.css import remove_unused_css
from calibre.ebooks.oeb.polish.replace import remove_links_to
from calibre.ebooks.oeb.polish.split import multisplit
from calibre.utils.logging import Log
from lxml import etree

from .xteink.archive import open_archive
from .xteink.images import FORMATS
from .xteink.io_utils import check_cancel


FONT_MEDIA = frozenset({
    "application/font-woff", "application/font-woff2", "application/vnd.ms-opentype",
    "application/x-font-opentype", "application/x-font-ttf", "application/x-font-otf",
    "font/ttf", "font/otf", "font/woff", "font/woff2", "font/sfnt",
})
FONT_SUFFIXES = frozenset({".woff", ".woff2", ".ttf", ".otf"})
PRUNE_SAFE_MEDIA = set(FORMATS) | FONT_MEDIA | OEB_DOCS | OEB_STYLES | {"application/x-dtbncx+xml"}
SPLIT_LIMIT = 32


def _drop_empty_font_faces(sheet):
    changed = False
    for index in range(sheet.cssRules.length - 1, -1, -1):
        rule = sheet.cssRules[index]
        if rule.type == rule.FONT_FACE_RULE and not rule.style.getPropertyValue("src"):
            sheet.deleteRule(index)
            changed = True
        elif hasattr(rule, "cssRules") and _drop_empty_font_faces(rule):
            changed = True
    return changed


def _remove_fonts(container, publication, cancel, report):
    candidates = {
        item.name for item in publication.resources if item.name
        and (item.media_type in FONT_MEDIA or Path(item.name).suffix.lower() in FONT_SUFFIXES)
    }
    # Keep manifest entries used outside font-face CSS, for example a link
    # to a downloadable font or an object in a chapter.
    fonts = candidates - publication.references
    if candidates - fonts:
        report("Retained embedded fonts linked directly from the publication.")
    if not fonts:
        return
    check_cancel(cancel)
    changed = remove_links_to(container, lambda name, href, fragment: name in fonts)
    for name in changed:
        media_type = container.mime_map[name]
        if media_type in OEB_STYLES:
            _drop_empty_font_faces(container.parsed(name))
        elif media_type in OEB_DOCS:
            for style in container.parsed(name).xpath('//*[local-name()="style"]'):
                if style.text and style.get("type", "text/css") == "text/css":
                    sheet = container.parse_css(style.text)
                    if _drop_empty_font_faces(sheet):
                        style.text = sheet.cssText.decode("utf-8")
    for font in sorted(fonts):
        check_cancel(cancel)
        container.remove_item(font)


def _prune_resources(container, publication, cancel, report):
    if any(resource.media_type not in PRUNE_SAFE_MEDIA for resource in publication.resources):
        report("Retained resources because this EPUB uses SVG or other media outside the unused-image check.")
        return
    in_use = set(publication.references) | publication.covers
    for name, media_type in list(container.mime_map.items()):
        check_cancel(cancel)
        if media_type in OEB_DOCS | OEB_STYLES:
            in_use.update(
                container.href_to_name(href, name) for href in container.iterlinks(name, get_line_numbers=False) if href
            )
    for resource in publication.resources:
        if resource.name and resource.media_type in FORMATS and not resource.properties and resource.name not in in_use:
            check_cancel(cancel)
            container.remove_item(resource.name)


def _split_documents(container, options, cancel, report):
    threshold = options.split_kib * 1024
    spine = {name for name, _ in container.spine_names}
    # Single body-level breaks avoid splitting tables, lists or note pairs
    # nested inside a block. Positional CSS may change at document boundaries.
    for name in sorted(spine):
        check_cancel(cancel)
        if container.mime_map.get(name) != "application/xhtml+xml" or container.filesize(name) <= threshold:
            continue
        body = container.parsed(name).find("{http://www.w3.org/1999/xhtml}body")
        if body is None or len(body) < 2:
            report(f"Kept {name} intact: no safe chapter split point.")
            continue
        size = 0
        positions = []
        oversized_block = False
        elements = (child for child in body if isinstance(child.tag, str))
        for position, child in enumerate(elements, 1):
            check_cancel(cancel)
            length = len(etree.tostring(child, encoding="utf-8"))
            oversized_block |= length > threshold
            if position > 1 and size + length > threshold:
                positions.append(position)
                size = 0
            size += length
        if not positions or len(positions) > SPLIT_LIMIT:
            report(f"Kept {name} intact: no safe split within the {SPLIT_LIMIT}-split limit.")
            continue
        points = " or ".join(f"position()={position}" for position in positions)
        multisplit(container, name, f"//h:body/*[{points}]", before=True)
        if oversized_block:
            report(f"One or more blocks in {name} remain larger than the split threshold.")


def edit_structure(source, output, workspace, publication, options, cancel, report):
    if publication.advanced_unsafe:
        report("Kept the original structure because of scripting, external resources, base URIs, CFI links, media overlays, fallbacks or fixed layout.")
        return False
    # Calibre parses and caches document trees during whole-book editing.
    # Keep that optional cost bounded even for a valid 1 GiB publication.
    with open_archive(source) as archive:
        text_size = sum(
            entry.file_size for entry in archive.infolist()
            if entry.filename.lower().endswith((".html", ".xhtml", ".htm", ".xml", ".opf", ".ncx", ".css", ".svg"))
        )
    if text_size > 48 * 1024 * 1024:
        report("Kept the original structure because its markup exceeds the Calibre editing limit.")
        return False
    check_cancel(cancel)
    directory = workspace / "calibre"
    directory.mkdir()
    container = get_container(str(source), log=Log(level=Log.ERROR), tdir=str(directory))
    has_svg = any(item.media_type == "image/svg+xml" for item in publication.resources)
    if options.remove_unused_css and not options.preserve_styling:
        if has_svg:
            report("Retained CSS because standalone SVG documents can use stylesheet rules.")
        else:
            check_cancel(cancel)
            remove_unused_css(container)
    if options.remove_fonts and not options.preserve_styling:
        if has_svg:
            report("Retained embedded fonts because standalone SVG documents can reference them.")
        else:
            _remove_fonts(container, publication, cancel, report)
    previous_count = len(container.mime_map)
    if options.remove_unused_resources:
        _prune_resources(container, publication, cancel, report)
    if options.split_xhtml:
        _split_documents(container, options, cancel, report)
    if not container.dirtied and previous_count == len(container.mime_map):
        return False
    check_cancel(cancel)
    container.commit(outpath=str(output))
    return True
