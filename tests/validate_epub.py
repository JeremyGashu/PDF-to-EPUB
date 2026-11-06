"""Structural EPUB checks: python tests/validate_epub.py book.epub [...]

Verifies the container layout, XML well-formedness of every XHTML/OPF/NCX file,
manifest <-> archive consistency, unique ids per document, and that every internal
href (content, nav, NCX) points at an existing file and fragment.
"""
import posixpath
import re
import sys
import zipfile
from urllib.parse import unquote
from xml.etree import ElementTree as ET

NS = {"opf": "http://www.idpf.org/2007/opf", "x": "http://www.w3.org/1999/xhtml",
      "ncx": "http://www.daisy.org/z3986/2005/ncx/"}


def check(path: str) -> list[str]:
    errs: list[str] = []
    z = zipfile.ZipFile(path)
    names = z.namelist()
    if names[0] != "mimetype" or z.read("mimetype") != b"application/epub+zip":
        errs.append("mimetype must be the first, uncompressed entry")
    if z.getinfo("mimetype").compress_type != zipfile.ZIP_STORED:
        errs.append("mimetype is compressed")
    container = ET.fromstring(z.read("META-INF/container.xml"))
    opf_path = container.find(".//{urn:oasis:names:tc:opendocument:xmlns:container}rootfile").get("full-path")
    base = posixpath.dirname(opf_path)
    opf = ET.fromstring(z.read(opf_path))
    manifest = {}
    for it in opf.findall(".//opf:manifest/opf:item", NS):
        full = posixpath.normpath(posixpath.join(base, it.get("href")))
        manifest[it.get("id")] = full
        if full not in names:
            errs.append(f"manifest item missing from archive: {full}")
    listed = set(manifest.values()) | {opf_path, "mimetype", "META-INF/container.xml"}
    for n in names:
        if n not in listed and not n.endswith("/"):
            errs.append(f"file not in manifest: {n}")
    for ref in opf.findall(".//opf:spine/opf:itemref", NS):
        if ref.get("idref") not in manifest:
            errs.append(f"spine idref unknown: {ref.get('idref')}")
    if not opf.findall(".//opf:item[@properties='nav']", NS):
        errs.append("no nav document")

    ids: dict[str, set[str]] = {}
    docs: dict[str, ET.Element] = {}
    for n in names:
        if n.endswith((".xhtml", ".ncx", ".opf")):
            try:
                root = ET.fromstring(z.read(n))
            except ET.ParseError as e:
                errs.append(f"{n}: not well-formed XML: {e}")
                continue
            docs[n] = root
            seen: set[str] = set()
            for el in root.iter():
                i = el.get("id")
                if i is None:
                    continue
                if i in seen:
                    errs.append(f"{n}: duplicate id {i}")
                if not re.fullmatch(r"[A-Za-z_][\w.\-]*", i):
                    errs.append(f"{n}: invalid id {i!r}")
                seen.add(i)
            ids[n] = seen

    def resolve(src: str, href: str) -> None:
        if re.match(r"^[a-z]+:", href):
            return
        file, _, frag = href.partition("#")
        target = posixpath.normpath(posixpath.join(posixpath.dirname(src), unquote(file))) if file else src
        if target not in names:
            errs.append(f"{src}: broken link {href}")
        elif frag and frag not in ids.get(target, set()):
            errs.append(f"{src}: missing fragment {href}")

    for n, root in docs.items():
        for el in root.iter():
            for attr in ("href", "src"):
                v = el.get(attr)
                if v and not n.endswith(".opf"):
                    resolve(n, v)
        if n.endswith(".xhtml"):
            body = root.find("x:body", NS)
            if body is None:
                errs.append(f"{n}: no body")
    return errs


if __name__ == "__main__":
    bad = 0
    for p in sys.argv[1:]:
        errs = check(p)
        print(f"{p}: {'OK' if not errs else f'{len(errs)} problem(s)'}")
        for e in errs[:40]:
            print("   ", e)
        bad += bool(errs)
    sys.exit(1 if bad else 0)
