"""Check EPUB package structure and local XML links without fetching resources."""

from dataclasses import dataclass
import os
from pathlib import Path
import posixpath
import re
import shutil
import subprocess
import tempfile
from time import monotonic
from urllib.parse import unquote, urlsplit

from lxml import etree

from .archive import inspect_archive, open_archive
from .io_utils import OptimisationError, check_cancel


OCF = "{urn:oasis:names:tc:opendocument:xmlns:container}"
OPF = "{http://www.idpf.org/2007/opf}"
EPUB = "{http://www.idpf.org/2007/ops}"
XHTML = "{http://www.w3.org/1999/xhtml}"
NCX = "{http://www.daisy.org/z3986/2005/ncx/}"
XML = "{http://www.w3.org/XML/1998/namespace}"
XLINK = "{http://www.w3.org/1999/xlink}"
MARKUP = {
    "application/xhtml+xml", "application/x-dtbncx+xml", "application/smil+xml",
    "image/svg+xml", "application/x-dtbook+xml",
}
MAX_XML = 24 * 1024 * 1024
MAX_FRAGMENT_LINKS = 200_000


@dataclass(frozen=True)
class Resource:
    identifier: str
    name: str | None
    media_type: str
    properties: frozenset


@dataclass
class Publication:
    resources: tuple
    covers: frozenset
    normalise_zip: bool
    advanced_unsafe: bool
    references: set[str]


def xml_tree(data, name):
    parser = etree.XMLParser(
        resolve_entities=False, no_network=True, load_dtd=False, recover=False,
        collect_ids=False,
    )
    try:
        root = etree.fromstring(data, parser=parser)
    except etree.XMLSyntaxError as error:
        raise OptimisationError(f"Malformed XML in {name}: {error}") from error
    # External XHTML doctypes are common in EPUB 2. Custom entities and DTD
    # declarations are never expanded and must not reach downstream tools.
    dtd = root.getroottree().docinfo.internalDTD
    if dtd is not None and (dtd.entities() or dtd.elements()):
        raise OptimisationError(f"Custom DTD declarations are unsupported in {name}.")
    return root


def read_xml(archive, entry):
    if entry.file_size > MAX_XML:
        raise OptimisationError(f"XML size limit exceeded in {entry.filename!r}.")
    return xml_tree(archive.read(entry), entry.filename)


def resolve_href(source, href):
    """Return an internal member and decoded fragment, or None for an external URI."""
    try:
        url = urlsplit(href.strip())
        if url.scheme or url.netloc or href.startswith("//"):
            return None
        path = unquote(url.path, errors="strict")
        fragment = unquote(url.fragment, errors="strict")
    except (ValueError, UnicodeError) as error:
        raise OptimisationError(f"Invalid EPUB URI {href!r} in {source}.") from error
    if "\\" in path or path.startswith("/") or "\x00" in path:
        raise OptimisationError(f"Unsafe EPUB URI {href!r} in {source}.")
    if path:
        target = posixpath.normpath(posixpath.join(posixpath.dirname(source), path))
    else:
        target = source
    if target in ("", ".", "..") or target.startswith("../"):
        raise OptimisationError(f"EPUB URI points outside the container: {href!r} in {source}.")
    return target, fragment


def _require_target(names, source, href):
    link = resolve_href(source, href)
    if link is not None and link[0] not in names:
        raise OptimisationError(f"Missing EPUB resource {link[0]!r} referenced by {source}.")
    return link


def _markup_attributes(root):
    for node in root.iter():
        if not isinstance(node.tag, str):
            continue
        attrs = ("href", XLINK + "href", "src", "poster")
        if node.tag == XHTML + "object":
            attrs += ("data",)
        for attr in attrs:
            href = node.get(attr)
            if href:
                yield node, href


def read_publication(archive, *, normalise=False, cancel=None):
    entries, normalise_zip = inspect_archive(archive, normalise, cancel)
    container_entry = entries.get("META-INF/container.xml")
    if container_entry is None:
        raise OptimisationError("The EPUB is missing META-INF/container.xml.")
    container = read_xml(archive, container_entry)
    if container.tag != OCF + "container":
        raise OptimisationError("Invalid EPUB container document.")
    rootfiles = container.findall(f"{OCF}rootfiles/{OCF}rootfile")
    if len(rootfiles) != 1:
        raise OptimisationError("An EPUB with a single package document is required.")
    rootfile = rootfiles[0]
    link = resolve_href("mimetype", rootfile.get("full-path", ""))
    opf = link[0] if link and not link[1] else ""
    if opf not in entries or rootfile.get("media-type") != "application/oebps-package+xml":
        raise OptimisationError("The EPUB package document is missing or invalid.")
    package = read_xml(archive, entries[opf])
    version = package.get("version", "").split(".")[0]
    manifest, spine = package.find(OPF + "manifest"), package.find(OPF + "spine")
    metadata = package.find(OPF + "metadata")
    if package.tag != OPF + "package" or version not in ("2", "3") or any(
        item is None for item in (manifest, spine, metadata)
    ):
        raise OptimisationError("Expected an EPUB 2 or EPUB 3 package with metadata, manifest and spine.")
    if any(node.get(XML + "base") is not None for node in package.iter() if isinstance(node.tag, str)):
        raise OptimisationError("Package-level xml:base URIs are unsupported.")

    resources = {}
    references = set()
    media_by_name = {}
    covers = set()
    nav = []
    advanced_unsafe = False
    for item in manifest.findall(OPF + "item"):
        check_cancel(cancel)
        identifier, href, media_type = item.get("id"), item.get("href"), item.get("media-type")
        if not identifier or identifier in resources or not href or not media_type:
            raise OptimisationError("Missing or duplicate EPUB manifest attributes.")
        link = _require_target(entries, opf, href)
        name = link[0] if link else None
        if link and (link[1] or name in media_by_name):
            raise OptimisationError(f"Ambiguous manifest resource {href!r}.")
        properties = frozenset(item.get("properties", "").split())
        if name is not None:
            media_by_name[name] = media_type
        if "cover-image" in properties and name:
            covers.add(name)
        if "nav" in properties:
            nav.append(name)
        if not name or properties & {"scripted", "remote-resources"} or any(
            item.get(key) for key in ("fallback", "fallback-style", "media-overlay")
        ) or media_type in ("application/javascript", "text/javascript", "application/smil+xml"):
            advanced_unsafe = True
        resources[identifier] = Resource(identifier, name, media_type, properties)

    for cover in metadata.findall(OPF + "meta"):
        if cover.get("name") == "cover":
            ref = resources.get(cover.get("content", ""))
            if ref and ref.name:
                covers.add(ref.name)
        if cover.get("property") == "rendition:layout" and "".join(cover.itertext()).strip() == "pre-paginated":
            advanced_unsafe = True

    spine_names = set()
    for itemref in spine.findall(OPF + "itemref"):
        resource = resources.get(itemref.get("idref"))
        if resource is None or resource.name is None:
            raise OptimisationError("An EPUB spine item is missing from the local manifest.")
        spine_names.add(resource.name)
        if "rendition:layout-pre-paginated" in itemref.get("properties", "").split():
            advanced_unsafe = True
    if not spine_names:
        raise OptimisationError("The EPUB spine is empty.")
    references.update(spine_names)

    ncx = resources.get(spine.get("toc"))
    if spine.get("toc") or version == "2":
        if ncx is None or ncx.media_type != "application/x-dtbncx+xml" or not ncx.name:
            raise OptimisationError("Missing or invalid NCX table of contents.")
        references.add(ncx.name)
    if version == "3" and (len(nav) != 1 or media_by_name.get(nav[0]) != "application/xhtml+xml"):
        raise OptimisationError("The EPUB 3 package must identify one XHTML navigation document.")
    references.update(name for name in nav if name)

    for node, href in _markup_attributes(package):
        if node.tag != OPF + "item":
            link = _require_target(entries, opf, href)
            if link:
                references.add(link[0])
                if node.tag == OPF + "reference" and "cover" in node.get("type", "").split():
                    covers.add(link[0])

    anchors = {}
    fragment_links = set()
    anchor_count = 0
    cover_links = {}
    for name, media_type in media_by_name.items():
        check_cancel(cancel)
        if media_type not in MARKUP:
            continue
        document = read_xml(archive, entries[name])
        if media_type == "application/xhtml+xml" and (
            document.tag != XHTML + "html" or document.find(XHTML + "body") is None
        ):
            raise OptimisationError(f"Invalid XHTML document: {name!r}.")
        is_nav = name in nav
        is_ncx = ncx is not None and name == ncx.name
        if is_nav and (
            document.tag != XHTML + "html" or not any(
                "toc" in node.get(EPUB + "type", "").split() for node in document.iter(XHTML + "nav")
            )
        ):
            raise OptimisationError("The EPUB navigation document is missing its table of contents.")
        if is_ncx and (document.tag != NCX + "ncx" or document.find(NCX + "navMap") is None):
            raise OptimisationError("The NCX document has no navigation map.")
        if any(
            node.get(XML + "base") is not None or node.tag == XHTML + "base"
            for node in document.iter() if isinstance(node.tag, str)
        ):
            # Browser base-URI semantics are preserved by the image-only path.
            advanced_unsafe = True
            continue
        if document.xpath('//*[local-name()="script" or @srcset or @onclick or @onload]'):
            advanced_unsafe = True
        if media_type in ("application/xhtml+xml", "image/svg+xml"):
            anchors[name] = set(document.xpath("//@id | //@xml:id | //h:a/@name", namespaces={"h": XHTML[1:-1]}))
            anchor_count += len(anchors[name])
            if anchor_count > 500_000:
                raise OptimisationError("The EPUB exceeds the supported anchor count.")
        for node, href in _markup_attributes(document):
            link = _require_target(entries, name, href)
            if not link:
                continue
            target, fragment = link
            references.add(target)
            if fragment.startswith("epubcfi("):
                advanced_unsafe = True
            if (is_nav or is_ncx or node.tag == XHTML + "a") and fragment and not fragment.startswith(
                ("epubcfi(", "svgView(", "t=", "xywh=", ":~:text=")
            ):
                fragment_links.add((name, target, fragment))
            if name in covers and media_by_name.get(target, "").startswith("image/"):
                cover_links.setdefault(name, set()).add(target)
        if len(fragment_links) > MAX_FRAGMENT_LINKS:
            raise OptimisationError("The EPUB exceeds the internal fragment link limit.")

    for source, target, fragment in fragment_links:
        if target in anchors and fragment not in anchors[target]:
            kind = "navigation" if source in nav or (ncx and source == ncx.name) else "internal"
            raise OptimisationError(f"Missing {kind} anchor {fragment!r} in {target} (from {source}).")
    for images in cover_links.values():
        covers.update(images)
    return entries, Publication(tuple(resources.values()), frozenset(covers), normalise_zip, advanced_unsafe, references)


def validate_epub(path, cancel=None):
    with open_archive(path) as archive:
        _, publication = read_publication(archive, cancel=cancel)
    return publication


def find_epubcheck(path=""):
    if not path:
        executable = shutil.which("epubcheck")
        return (executable,) if executable else ()
    configured = Path(path).expanduser()
    if not configured.is_file() or not configured.is_absolute():
        raise OptimisationError("Choose the full path to EPUBCheck or its .jar file.")
    if configured.suffix.lower() == ".jar":
        java = shutil.which("java")
        if not java:
            raise OptimisationError("Java is required to run the configured EPUBCheck .jar.")
        return (java, "-jar", str(configured))
    if os.name == "nt" and configured.suffix.lower() != ".exe":
        raise OptimisationError("Choose an EPUBCheck .jar or .exe on Windows.")
    return (str(configured),)


def validator_identity(command):
    if not command:
        return ()
    details = []
    for path in command:
        if not path.startswith("-"):
            stat = os.stat(path)
            details.append((path, stat.st_size, stat.st_mtime_ns))
    return tuple(details)


def run_epubcheck(command, path, cancel=None, timeout=180):
    if not command:
        return "EPUBCheck was unavailable. The built-in structural checks passed."
    with tempfile.TemporaryFile("w+b") as log:
        with subprocess.Popen(
            [*command, str(path)], stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        ) as process:
            deadline = monotonic() + timeout
            try:
                while True:
                    check_cancel(cancel)
                    try:
                        status = process.wait(timeout=0.2)
                        break
                    except subprocess.TimeoutExpired:
                        if monotonic() >= deadline:
                            raise OptimisationError(f"EPUBCheck timed out after {timeout} seconds.")
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
        size = log.tell()
        log.seek(max(0, size - 14000))
        details = log.read().decode("utf-8", errors="replace").strip()
        if status:
            raise OptimisationError(f"EPUBCheck exited with code {status}.\n{details}")
        warnings = [line for line in details.splitlines() if line.lstrip().startswith("WARNING")]
        return "\n".join(warnings)
