#!/usr/bin/env python3
"""
Optional branding: a logo, name and accent color shown in the Lex UI.

The brand lives in a user-supplied directory (default: brand/ next to server.py, git-ignored; override with
`server.py --brand DIR` or LEX_BRAND_DIR). It holds one manifest, brand.json, plus the image files it names:

    {
      "name": "Acme Platform",          // required, up to 60 characters
      "tagline": "Production clusters", // optional, up to 120 characters
      "logo": "logo.svg",               // optional: shown on Lex's dark UI (top bar, Jumbotron, report cards)
      "logoOnLight": "logo-dark.svg",   // optional: for light backgrounds (downloaded report cards in light mode)
      "favicon": "favicon.png",         // optional: browser tab icon
      "accentColor": "#7c5cff",         // optional: #rrggbb, used for brand trim
      "url": "https://intranet.example.com/platform"   // optional: where clicking the logo goes (http/https)
    }

Image files must sit directly in the brand directory (no paths), be .svg, .png, .jpg, .jpeg, .webp or .ico,
and be at most 2 MB. Only files named by the manifest are ever served. Changes are picked up without a restart.
"""

import os
import re
import json
import threading

APP_DIR = os.path.dirname(os.path.abspath(__file__))
BRAND_DIR = os.environ.get("LEX_BRAND_DIR") or os.path.join(APP_DIR, "brand")
MANIFEST = "brand.json"
MAX_ASSET_BYTES = 2 * 1024 * 1024
MAX_MANIFEST_BYTES = 64 * 1024
CONTENT_TYPES = {".svg": "image/svg+xml", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                 ".webp": "image/webp", ".ico": "image/x-icon"}
ASSET_FIELDS = ("logo", "logoOnLight", "favicon")
FILE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
URL_RE = re.compile(r"^https?://[^\s<>\"']{1,500}$")

_cache = {"key": None, "value": None}
_lock = threading.Lock()


def set_brand_dir(path):
    global BRAND_DIR
    BRAND_DIR = os.path.abspath(path)
    with _lock:
        _cache["key"], _cache["value"] = None, None


def _manifest_key():
    """Cache key: the manifest's and its assets' modification times (so edits apply without a restart)."""
    path = os.path.join(BRAND_DIR, MANIFEST)
    try:
        st = os.stat(path)
    except OSError:
        return None
    names = sorted(n for n in os.listdir(BRAND_DIR) if os.path.splitext(n)[1].lower() in CONTENT_TYPES) if os.path.isdir(BRAND_DIR) else []
    mtimes = []
    for n in names:
        try:
            mtimes.append((n, os.stat(os.path.join(BRAND_DIR, n)).st_mtime_ns))
        except OSError:
            pass
    return (st.st_mtime_ns, st.st_size, tuple(mtimes))


def _validate(raw):
    """Returns (brand for the browser, {file name: absolute path}, [problems])."""
    problems = []
    if not isinstance(raw, dict):
        return None, {}, ["brand.json must contain a JSON object"]

    def text(field, limit, required=False):
        v = raw.get(field)
        if v is None:
            if required:
                problems.append(f'"{field}" is required')
            return None
        if not isinstance(v, str) or not v.strip():
            problems.append(f'"{field}" must be a non-empty string')
            return None
        v = " ".join(v.split())
        if len(v) > limit:
            problems.append(f'"{field}" is longer than {limit} characters; truncated')
            v = v[:limit]
        return v

    brand = {"name": text("name", 60, required=True), "tagline": text("tagline", 120)}
    if brand["name"] is None:
        return None, {}, problems

    color = raw.get("accentColor")
    if color is not None:
        if isinstance(color, str) and COLOR_RE.match(color):
            brand["accentColor"] = color.lower()
        else:
            problems.append('"accentColor" must look like "#rrggbb"; ignored')

    url = raw.get("url")
    if url is not None:
        if isinstance(url, str) and URL_RE.match(url):
            brand["url"] = url
        else:
            problems.append('"url" must be an http(s) URL; ignored')

    assets = {}
    for field in ASSET_FIELDS:
        name = raw.get(field)
        if name is None:
            continue
        if not isinstance(name, str) or not FILE_NAME_RE.match(name) or name == MANIFEST:
            problems.append(f'"{field}" must be a file name in the brand directory (no folders); ignored')
            continue
        ext = os.path.splitext(name)[1].lower()
        if ext not in CONTENT_TYPES:
            problems.append(f'"{field}": {name} must be one of {", ".join(sorted(CONTENT_TYPES))}; ignored')
            continue
        path = os.path.join(BRAND_DIR, name)
        try:
            st = os.stat(path)
        except OSError:
            problems.append(f'"{field}": {name} was not found in {BRAND_DIR}; ignored')
            continue
        if not os.path.isfile(path) or os.path.islink(path):
            problems.append(f'"{field}": {name} must be a regular file; ignored')
            continue
        if st.st_size > MAX_ASSET_BYTES:
            problems.append(f'"{field}": {name} is larger than {MAX_ASSET_BYTES // (1024 * 1024)} MB; ignored')
            continue
        assets[name] = path
        brand[field] = f"/brand/{name}?v={st.st_mtime_ns}"   # cache-busting version

    unknown = sorted(set(raw) - {"name", "tagline", "accentColor", "url", *ASSET_FIELDS, "$schema", "_comment"})
    if unknown:
        problems.append(f"unknown field(s) ignored: {', '.join(unknown)}")
    return {k: v for k, v in brand.items() if v is not None}, assets, problems


def load():
    """{"brand": {...} or None, "assets": {name: path}, "problems": [...], "dir": BRAND_DIR}, cached by mtime."""
    key = _manifest_key()
    with _lock:
        if _cache["key"] == key and _cache["value"] is not None:
            return _cache["value"]
    if key is None:
        value = {"brand": None, "assets": {}, "problems": [], "dir": BRAND_DIR}
    else:
        path = os.path.join(BRAND_DIR, MANIFEST)
        try:
            if os.path.getsize(path) > MAX_MANIFEST_BYTES:
                raise ValueError(f"brand.json is larger than {MAX_MANIFEST_BYTES // 1024} KB")
            with open(path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            brand, assets, problems = _validate(raw)
        except (OSError, ValueError) as e:
            brand, assets, problems = None, {}, [f"could not read brand.json: {e}"]
        value = {"brand": brand, "assets": assets, "problems": problems, "dir": BRAND_DIR}
    with _lock:
        _cache["key"], _cache["value"] = key, value
    return value


def asset(name):
    """(bytes, content type) for a file the current manifest names, else None."""
    info = load()
    path = info["assets"].get(name)
    if not path:
        return None
    try:
        with open(path, "rb") as f:
            data = f.read(MAX_ASSET_BYTES + 1)
    except OSError:
        return None
    if len(data) > MAX_ASSET_BYTES:
        return None
    return data, CONTENT_TYPES[os.path.splitext(name)[1].lower()]


def describe():
    """One line for the server's startup log."""
    info = load()
    lines = []
    if info["brand"]:
        parts = [k for k in ASSET_FIELDS if k in info["brand"]]
        lines.append(f"✔ Brand: {info['brand']['name']} ({info['dir']}{', ' + ', '.join(parts) if parts else ''})")
    elif os.path.exists(os.path.join(info["dir"], MANIFEST)):
        lines.append(f"▲ Brand in {info['dir']} was not loaded")
    for p in info["problems"]:
        lines.append(f"▲ brand.json: {p}")
    return lines
