"""Synthetic DRM-free EPUB fixtures with small, predictable resources."""

from io import BytesIO
from pathlib import Path
from random import Random
from urllib.parse import quote
from xml.sax.saxutils import escape
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile, ZipInfo

from PIL import Image


MIME = b"application/epub+zip"
CONTAINER = """<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles><rootfile full-path="EPUB/本.opf" media-type="application/oebps-package+xml"/></rootfiles>
</container>"""
UID = "urn:uuid:8839b64a-ec89-49a3-82e9-54a04ba501b2"
CSS = b'''
@media screen { .text { font-size: 1em; } }
.text, .unused { line-height: 1.5; }
.unused { color: red; }
a:hover { color: #013b55; }
'''
SVG = b'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 90 40">
<!-- a long comment that can be removed without changing the vector illustration -->
<title>Vector illustration</title><rect width="90" height="40" fill="#516966"/>
</svg>'''


def image_bytes(format="JPEG", mode="RGB", size=(64, 40), **kwargs):
    if mode == "RGB":
        image = Image.frombytes("RGB", size, Random(21).randbytes(size[0] * size[1] * 3))
    elif mode == "RGBA":
        image = Image.new("RGBA", size, (125, 52, 61, 0))
        image.paste((0, 166, 54, 120), (2, 2, size[0] - 2, size[1] - 2))
    else:
        image = Image.new(mode, size, 127)
    with BytesIO() as output:
        image.save(output, format, **kwargs)
        return output.getvalue()


def make_epub(
    path, *, version=3, images=None, cover=None, css=None, font=False, large=False,
    malformed=None, extra=None, stored_first=True, scripted=False, math=False, nav_fragment="b",
):
    path = Path(path)
    images = images or {}
    resources = {"EPUB/text/chapter.xhtml": None, "EPUB/text/notes.xhtml": None}
    properties = ' '.join(name for name, enabled in (('scripted', scripted), ('mathml', math)) if enabled)
    manifest = [
        ('a', 'text/chapter.xhtml', 'application/xhtml+xml', properties),
        ('b', 'text/notes.xhtml', 'application/xhtml+xml', ''),
    ]
    if css is not None:
        manifest.append(("css", "theme.css", "text/css", ""))
        resources["EPUB/theme.css"] = css
    if font:
        manifest.append(("font", "fonts/test.ttf", "font/ttf", ""))
        resources["EPUB/fonts/test.ttf"] = b"\0\1\0\0synthetic test font"
    cover_id = None
    image_tags = []
    for index, (filename, (media_type, data, use)) in enumerate(images.items()):
        identifier = f"img{index}"
        is_cover = filename == cover
        if is_cover:
            cover_id = identifier
        manifest.append((identifier, quote(filename), media_type, "cover-image" if version == 3 and is_cover else ""))
        resources["EPUB/" + filename] = data
        if use:
            image_tags.append(f'<p><img src="../{quote(filename)}" alt="Illustration {index}"/></p>')
    extra_meta = f'<meta name="cover" content="{cover_id}"/>' if version == 2 and cover_id else ""
    if version == 3:
        manifest.append(("toc", "nav.xhtml", "application/xhtml+xml", "nav"))
        resources["EPUB/nav.xhtml"] = f'''<html xmlns="http://www.w3.org/1999/xhtml"
 xmlns:epub="http://www.idpf.org/2007/ops" lang="ja" xml:lang="ja">
<head><title>目次</title></head><body><nav epub:type="toc"><ol>
<li><a href="text/chapter.xhtml#a">第一章</a></li><li><a href="text/notes.xhtml#{nav_fragment}">Notas</a></li>
</ol></nav></body></html>'''
        version_string, toc = "3.0", ""
        extra_meta += '<meta property="dcterms:modified">2026-11-23T00:00:00Z</meta>'
    else:
        manifest.append(("toc", "toc.ncx", "application/x-dtbncx+xml", ""))
        resources["EPUB/toc.ncx"] = f'''<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">
<head><meta name="dtb:uid" content="{UID}"/><meta name="dtb:depth" content="1"/>
<meta name="dtb:totalPageCount" content="0"/><meta name="dtb:maxPageNumber" content="0"/></head>
<docTitle><text>旅と海</text></docTitle><navMap>
<navPoint id="first" playOrder="1"><navLabel><text>第一章</text></navLabel><content src="text/chapter.xhtml#a"/></navPoint>
<navPoint id="notes" playOrder="2"><navLabel><text>Notas</text></navLabel><content src="text/notes.xhtml#{nav_fragment}"/></navPoint>
</navMap></ncx>'''
        version_string, toc = "2.0", ' toc="toc"'

    items = "\n".join(
        f'<item id="{identifier}" href="{name}" media-type="{mime}"'
        + (f' properties="{properties}"' if properties else "") + "/>"
        for identifier, name, mime, properties in manifest
    )
    package_language = ' xml:lang="ja"' if version == 3 else ""
    opf = f'''<package xmlns="http://www.idpf.org/2007/opf" unique-identifier="uid" version="{version_string}"{package_language}>
<metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:identifier id="uid">{UID}</dc:identifier>
<dc:title>旅と海</dc:title><dc:creator>Amélie García</dc:creator><dc:language>ja</dc:language>
{extra_meta}</metadata><manifest>{items}</manifest><spine{toc}><itemref idref="a"/><itemref idref="b"/></spine></package>'''
    lang = ' xml:lang="ja"' + (' lang="ja" xmlns:epub="http://www.idpf.org/2007/ops"' if version == 3 else "")
    head = '<head><title>旅と海</title>'
    if css is not None:
        head += '<link rel="stylesheet" type="text/css" href="../theme.css"/>'
    head += "</head>"
    body = '<h1 id="a">第一章</h1><p class="text">中文 العربية Tiếng Việt café'
    if version == 3:
        body += ' <ruby>海<rt>うみ</rt></ruby> <a href="notes.xhtml#b" epub:type="noteref">1</a></p>'
    else:
        body += ' <a href="notes.xhtml#b">1</a></p>'
    if math:
        body += '<p><math xmlns="http://www.w3.org/1998/Math/MathML"><mi>α</mi><mo>+</mo><mn>2</mn></math></p>'
    if large:
        text = escape("Une longue section du livre. " * 92)
        body += "".join(f'<p id="section-{i}">{text}</p>' for i in range(48))
        body += '<p><a href="notes.xhtml#b">More notes</a></p>'
    body += "".join(image_tags)
    start = f'<html xmlns="http://www.w3.org/1999/xhtml"{lang}>{head}<body>'
    resources["EPUB/text/chapter.xhtml"] = start + body + "</body></html>"
    chapter = "chapter.xhtml#section-38" if large else "chapter.xhtml#a"
    marker = ' epub:type="footnote"' if version == 3 else ""
    resources["EPUB/text/notes.xhtml"] = start + f'<div id="b"{marker}><p>Nota <a href="{chapter}">↩</a></p></div></body></html>'
    resources.update({"META-INF/container.xml": CONTAINER, "EPUB/本.opf": opf})
    resources.update(extra or {})
    if malformed:
        resources[malformed] = b'<malformed>'
    with ZipFile(path, "w", compression=ZIP_DEFLATED) as archive:
        if stored_first:
            archive.writestr(ZipInfo("mimetype"), MIME, compress_type=ZIP_STORED)
        for name, content in resources.items():
            archive.writestr(name, content)
        if not stored_first:
            archive.writestr("mimetype", MIME)
    return path
