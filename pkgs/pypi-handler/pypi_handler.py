# SPDX-License-Identifier: MIT
"""The HTTP handler of Python package indexes, for a purifier whose lock
run records a pip install that the build makes itself, as a CMake
configure that runs pip does (tools/http_records.py of the purifier
library describes the protocol).

It claims the pages of the simple repository API (PEP 503), on any
index, and the package files pip fetched, wheels and sdists, with the
core metadata files beside them (PEP 658). For each package file it
returns a file entry. For each page it returns an inline entry: the page
written again from the package files of its project that the lock run
fetched, each linked at its URL with its SHA-256, so that pip in the
sandbox resolves to the files it resolved to in the lock run. An index
rewrites a page whenever a project publishes a file, so a page as
recorded would go stale; the written page changes only when the lock
does. Core metadata files are claimed without an entry: the written
pages do not offer them, so pip fetches the package files.
"""
import base64
import html
import urllib.parse

import http_records
from packaging.utils import (
    InvalidSdistFilename,
    InvalidWheelFilename,
    canonicalize_name,
    parse_sdist_filename,
    parse_wheel_filename,
)


def page_project(url):
    """The project of a page of the simple repository API, the index root
    as "", or None for another URL: `.../simple/` and `.../simple/<name>/`."""
    parts = urllib.parse.urlsplit(url)
    components = [c for c in parts.path.split("/") if c]
    if components[-1:] == ["simple"]:
        return ""
    if components[-2:-1] == ["simple"] and parts.path.endswith("/"):
        return canonicalize_name(components[-1])
    return None


def package_project(url):
    """The project of a package file, by its file name, or None."""
    name = urllib.parse.unquote(urllib.parse.urlsplit(url).path.rsplit("/", 1)[-1])
    try:
        if name.endswith(".whl"):
            return canonicalize_name(parse_wheel_filename(name)[0])
        return canonicalize_name(parse_sdist_filename(name)[0])
    except (InvalidWheelFilename, InvalidSdistFilename):
        return None


def is_metadata(url):
    """Whether a URL is the core metadata file beside a wheel (PEP 658)."""
    path = urllib.parse.urlsplit(url).path
    return path.endswith(".metadata") and package_project(path[: -len(".metadata")]) is not None


def sha256_hex(sri):
    """The hex SHA-256 of an SRI sha256 hash."""
    algorithm, _, digest = sri.partition("-")
    if algorithm != "sha256":
        raise ValueError(f"not a SHA-256 hash: {sri}")
    return base64.b64decode(digest).hex()


def page(links):
    """A page of the simple repository API (PEP 503) linking the files."""
    body = "".join(f'    <a href="{html.escape(href)}">{html.escape(text)}</a><br/>\n' for href, text in links)
    return (
        "<!DOCTYPE html>\n<html>\n  <head>\n"
        '    <meta name="pypi:repository-version" content="1.0">\n'
        "  </head>\n  <body>\n" + body + "  </body>\n</html>\n"
    )


def handle(records):
    gets = http_records.successful_gets(records)
    pages = {r["url"]: page_project(r["url"]) for r in records if r["method"] == "GET"}
    pages = {url: project for url, project in pages.items() if project is not None}
    files = {url: package_project(url) for url in gets}
    files = {url: project for url, project in files.items() if project is not None}
    metadata = [url for url in gets if is_metadata(url)]
    by_project = {}
    for url, project in sorted(files.items()):
        by_project.setdefault(project, []).append(url)

    entries = [http_records.file_entry(url, gets[url]["hash"]) for url in files]
    for url, project in pages.items():
        if project == "":
            # The index root links the page of each project.
            links = [(f"{p}/", p) for p in sorted({p for p in pages.values() if p})]
        else:
            links = [
                (f"{f}#sha256={sha256_hex(gets[f]['hash'])}", urllib.parse.unquote(f.rsplit("/", 1)[-1]))
                for f in by_project.get(project, [])
            ]
        entries.append(http_records.inline_entry(http_records.index_path(url), page(links)))
    return [*pages, *files, *metadata], entries


if __name__ == "__main__":
    http_records.run(handle)
