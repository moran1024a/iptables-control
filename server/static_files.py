"""Built panel files, loaded once into a fixed set so every other path stays a 404."""

import gzip
import os
import re
import threading
import settings

TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
    ".webmanifest": "application/manifest+json",
}
COMPRESSIBLE = (".html", ".js", ".css", ".svg", ".webmanifest")
NAME = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9._-]*")
# The panel only talks to its own origin. Styles need 'unsafe-inline' for
# style attributes written by the UI runtime; scripts never do.
INDEX_HEADERS = [
    (
        "Content-Security-Policy",
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: blob:; font-src 'self' data:; connect-src 'self'; "
        "object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'",
    ),
    ("Referrer-Policy", "no-referrer"),
    ("X-Frame-Options", "DENY"),
]
_lock = threading.Lock()
_loaded = {}


def load(root):
    """Top-level files and one `assets` directory; names are never taken from a request."""
    found = {}
    for folder in ("", "assets"):
        base = os.path.join(root, folder)
        if not os.path.isdir(base):
            continue
        for entry in sorted(os.listdir(base)):
            path = os.path.join(base, entry)
            suffix = os.path.splitext(entry)[1]
            if (
                suffix not in TYPES
                or not NAME.fullmatch(entry)
                or os.path.islink(path)
                or not os.path.isfile(path)
            ):
                continue
            with open(path, "rb") as f:
                data = f.read()
            packed = None
            if suffix in COMPRESSIBLE and len(data) >= 1024:
                packed = gzip.compress(data, 9, mtime=0)
                if len(packed) >= len(data):
                    packed = None
            found[f"{folder}/{entry}" if folder else entry] = (TYPES[suffix], data, packed)
    return found


def files():
    root = settings.WEB_DIR
    with _lock:
        if root not in _loaded:
            _loaded.clear()
            _loaded[root] = load(root)
        return _loaded[root]


def response(name, accept_encoding=""):
    """Return (data, content type, headers) for a built file, or None."""
    entry = files().get(name)
    if entry is None:
        return None
    ctype, data, packed = entry
    headers = [("X-Content-Type-Options", "nosniff")]
    # Hashed build output never changes; the entry document is always revalidated.
    if name.startswith("assets/"):
        headers.append(("Cache-Control", "private, max-age=31536000, immutable"))
    else:
        headers.append(("Cache-Control", "no-cache"))
    if name == "index.html":
        headers += INDEX_HEADERS
    if packed is not None:
        headers.append(("Vary", "Accept-Encoding"))
        accepted = {part.split(";")[0].strip().lower() for part in accept_encoding.split(",")}
        if "gzip" in accepted:
            data = packed
            headers.append(("Content-Encoding", "gzip"))
    return data, ctype, headers
