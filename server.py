#!/usr/bin/env python3
"""
Lightweight Local API Server for Lex.
Serves web assets on http://127.0.0.1:8000, runs a background thread to
periodically query Kubernetes context, and exposes secure endpoints for pod troubleshooting.
"""

import os
import re
import json
import gzip
import time
import atexit
import calendar
import datetime
import posixpath
import subprocess
import threading
import http.server
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse, parse_qs, unquote
import parse_cluster
import dvr_db
import metrics
import redaction
import watcher
import usage
import diagnose
import brand
import archipelago

PORT = 8000
BIND_ADDRESS = '127.0.0.1'  # Hard-bound to local loopback for secure sandbox isolation
SYNC_INTERVAL_SECONDS = 30
# Bump whenever the state payload or API changes shape. The UI compares it with its own copy and asks the
# user to reload if they differ (e.g. a browser tab still running a pre-upgrade index.html).
API_SCHEMA_VERSION = 5
DVR_RETENTION_DAYS = 7
# Watch-based ingestion (list once, then stream changes). Off at module level so embedding/tests keep the
# simple polling path; main() turns it on unless --no-watch is given.
WATCH_ENABLED = False
WATCH_READY_TIMEOUT_SECONDS = 60
STREAM_HEARTBEAT_SECONDS = 15
GZIP_MIN_BYTES = 1024
APP_DIR = os.path.dirname(os.path.abspath(__file__))

# Only these paths are served as static files. Everything else in the working directory
# (raw kubectl dumps, compiled cluster state, the DVR database, .git) must never be exposed.
STATIC_FILES = {"/": "/index.html", "/index.html": "/index.html"}
STATIC_DIR_PREFIXES = ("/images/",)

# Hostnames accepted in the Host / Origin headers. Rejecting anything else blocks
# DNS-rebinding attacks where a malicious site resolves its own domain to 127.0.0.1.
ALLOWED_HOSTNAMES = ("127.0.0.1", "localhost", "[::1]")

# Kubernetes object name validation (DNS-1123). Names must start and end with an
# alphanumeric character, so values like "--insecure-skip-tls-verify" can never reach kubectl as flags.
DNS1123_SUBDOMAIN_RE = re.compile(r"^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$")  # pods, nodes
DNS1123_LABEL_RE = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")       # namespaces
CONTEXT_NAME_RE = re.compile(r"^[a-zA-Z0-9_.:@/][a-zA-Z0-9_./:@-]*$")

def is_valid_object_name(name):
    return bool(name) and len(name) <= 253 and bool(DNS1123_SUBDOMAIN_RE.match(name))

def is_valid_namespace(namespace):
    return bool(namespace) and len(namespace) <= 63 and bool(DNS1123_LABEL_RE.match(namespace))

def is_valid_context_name(context):
    return bool(context) and len(context) <= 512 and bool(CONTEXT_NAME_RE.match(context))

def list_kube_contexts():
    """Returns the context names from the local kubeconfig (empty list on failure)."""
    try:
        res = subprocess.run(["kubectl", "config", "get-contexts", "-o", "name"], capture_output=True, text=True, timeout=3)
    except Exception:
        return []
    if res.returncode != 0:
        return []
    return [line.strip() for line in res.stdout.split('\n') if line.strip()]

# Thread-safe context management
active_context = "demo"
active_context_lock = threading.Lock()

# DVR recordings: context -> session_id. Each recording is bound to the context it was started on and keeps
# recording it in the background whichever context the UI shows; several clusters can be recorded at once (O-05).
active_recordings = {}
active_recordings_lock = threading.Lock()

# Warm contexts (O-05): kept current in the background (a watcher each, or a parallel scrape in polling mode) so
# switching to them is instant. The active context, recorded contexts, contexts named with --warm, and the
# WARM_RECENT most recently used ones are warm.
WARM_CONTEXTS = set()
WARM_RECENT = 2
recent_contexts = []          # most recent first, never the active context
warm_lock = threading.Lock()

# F-36 Archipelago: contexts shown as islands next to the active one. They are warm, so islands stay current.
archipelago_members = []
archipelago_lock = threading.Lock()

def archipelago_contexts():
    with archipelago_lock:
        return list(archipelago_members)

def recordings():
    with active_recordings_lock:
        return dict(active_recordings)

def wanted_contexts():
    """Real (non-demo) contexts that should be kept current right now."""
    with active_context_lock:
        wanted = {active_context}
    wanted |= set(recordings())
    with warm_lock:
        wanted |= set(recent_contexts[:WARM_RECENT]) | WARM_CONTEXTS
    wanted |= set(archipelago_contexts())
    wanted.discard("demo")
    wanted.discard(archipelago.DEMO_ISLAND)
    return wanted

def remember_recent(previous, current):
    """Called on a context switch: the context we left stays warm for a while."""
    global recent_contexts
    with warm_lock:
        recent_contexts = ([previous] if previous not in (None, "demo", current) else []) + \
                          [c for c in recent_contexts if c not in (previous, current)]
        del recent_contexts[10:]

def context_ready(ctx):
    """Has warm, current state on disk (switching to it needs no kubectl round trip)."""
    if ctx == "demo":
        return True
    _, _, state_file = parse_cluster.get_file_paths(ctx)
    if not os.path.exists(state_file):
        return False
    if WATCH_ENABLED:
        w = get_watcher(ctx)
        return w is not None and w.first_compile.is_set() and w.thread.is_alive()
    status = get_sync_status(ctx)
    if not status.get("lastSuccessAt") or status.get("lastError"):
        return False
    age = time.time() - calendar.timegm(time.strptime(status["lastSuccessAt"], "%Y-%m-%dT%H:%M:%SZ"))
    return age < SYNC_INTERVAL_SECONDS * 2.5

# Per-context scrape health, surfaced to the UI so stale data is never presented as live
sync_status = {}
sync_status_lock = threading.Lock()

def utc_now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

def scrape_context(ctx, write_to_file=True):
    """Compiles fresh state for ctx and records success/failure in sync_status. Returns the state or None."""
    attempt_at = utc_now_iso()
    try:
        if ctx == "demo":
            state = parse_cluster.parse_cluster(force_mock=True, write_to_file=write_to_file)
        else:
            state = parse_cluster.parse_cluster(context=ctx, write_to_file=write_to_file)
        error = None
    except SystemExit:
        state, error = None, f"Context '{ctx}' is unreachable"
    except Exception as e:
        state, error = None, f"Error compiling cluster state: {e}"
    if state is not None and ctx != "demo":
        try:
            metrics.record(ctx, state)
        except Exception as e:
            print(f"▲ Could not record metrics for '{ctx}': {e}")
    with sync_status_lock:
        entry = sync_status.setdefault(ctx, {"lastSuccessAt": None})
        entry["lastAttemptAt"] = attempt_at
        entry["lastError"] = error
        if state is not None:
            entry["lastSuccessAt"] = attempt_at
    if error:
        print(f"▲ Sync failed for '{ctx}': {error}")
    return state

def get_sync_status(ctx):
    with sync_status_lock:
        status = dict(sync_status.get(ctx, {}))
    w = get_watcher(ctx)
    if w is not None:
        status["mode"] = w.mode
        if w.healthy_at:
            status["freshAt"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(w.healthy_at))
    return status

# ---------- Watch-based ingestion ----------
watchers = {}                      # context -> watcher.ContextWatcher
watchers_lock = threading.Lock()
# Bumped whenever the active context's state changes; /api/v1/stream tells browsers to re-fetch
state_change = threading.Condition()
state_version = 0
_last_content_hash = {}

def notify_state_change():
    global state_version
    with state_change:
        state_version += 1
        state_change.notify_all()

def _on_watch_compiled(ctx, state):
    now = utc_now_iso()
    with sync_status_lock:
        entry = sync_status.setdefault(ctx, {"lastSuccessAt": None})
        entry["lastAttemptAt"] = now
        entry["lastSuccessAt"] = now
        entry["lastError"] = None
    content_hash = state.get("contentHash")
    if _last_content_hash.get(ctx) != content_hash:
        _last_content_hash[ctx] = content_hash
        with active_context_lock:
            is_active = ctx == active_context
        if is_active:
            notify_state_change()

def _on_watch_error(ctx, message):
    with sync_status_lock:
        entry = sync_status.setdefault(ctx, {"lastSuccessAt": None})
        entry["lastAttemptAt"] = utc_now_iso()
        entry["lastError"] = message
    print(f"▲ Sync failed for '{ctx}': {message}")

def get_watcher(ctx):
    with watchers_lock:
        return watchers.get(ctx)

def ensure_watcher(ctx):
    """Starts (or returns) the watcher for a real context. A watcher whose thread died is replaced."""
    with watchers_lock:
        w = watchers.get(ctx)
        if w is None or not w.thread.is_alive():
            w = watcher.ContextWatcher(ctx, _on_watch_compiled, _on_watch_error, poll_interval=SYNC_INTERVAL_SECONDS).start()
            watchers[ctx] = w
            print(f"✔ Watching '{ctx}' for changes")
        return w

def stop_unneeded_watchers():
    """Only warm contexts (active, recorded, --warm, recently used) keep their watchers."""
    keep = wanted_contexts()
    with watchers_lock:
        stale = [c for c in watchers if c not in keep]
        stopped = [watchers.pop(c) for c in stale]
    for w in stopped:
        w.stop()
        print(f"Stopped watching '{w.context}'")

def stop_all_watchers():
    with watchers_lock:
        stopped = list(watchers.values())
        watchers.clear()
    for w in stopped:
        w.stop()

def read_state_file(ctx):
    _, _, state_file = parse_cluster.get_file_paths(ctx)
    with open(state_file, 'r', encoding='utf-8') as f:
        return json.load(f)

def refresh_context(ctx):
    """Makes sure ctx has current state on disk and returns it (None on failure, see sync_status).
    Polling mode scrapes now; watch mode starts the watcher if needed and waits for its first compile."""
    if not WATCH_ENABLED or ctx == "demo":
        if ctx != "demo" and ctx in wanted_contexts() and context_ready(ctx):
            try:
                return read_state_file(ctx)   # kept current by the parallel background scrapes
            except (OSError, ValueError):
                pass
        return scrape_context(ctx)
    w = ensure_watcher(ctx)
    if not w.wait_ready(WATCH_READY_TIMEOUT_SECONDS):
        if not get_sync_status(ctx).get("lastError"):
            _on_watch_error(ctx, w.last_error or f"Timed out after {WATCH_READY_TIMEOUT_SECONDS}s listing '{ctx}'")
        return None
    try:
        return read_state_file(ctx)
    except (OSError, ValueError) as e:
        _on_watch_error(ctx, f"Could not read compiled state: {e}")
        return None

_YAML_PLAIN_RE = re.compile(r"^[A-Za-z0-9_./@%+=-][A-Za-z0-9_./@%+=:, -]*$")
_YAML_RESERVED = {"", "true", "false", "yes", "no", "on", "off", "null", "~"}

def _yaml_scalar(v):
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    text = str(v)
    if (_YAML_PLAIN_RE.match(text) and text.lower() not in _YAML_RESERVED and ": " not in text
            and not text.endswith(":") and text.strip() == text and not re.match(r"^[0-9.+-]+$", text)):
        return text
    return json.dumps(text)   # JSON strings are valid YAML double-quoted scalars

def dict_to_yaml(d, indent=0):
    """YAML rendering of a JSON-like structure (keys in Kubernetes order, strings quoted when needed)."""
    lines = []
    spacer = " " * indent
    if isinstance(d, dict):
        keys = list(d.keys())
        preferred = ["apiVersion", "kind", "metadata", "spec", "status"]
        for k in [k for k in preferred if k in keys] + [k for k in keys if k not in preferred]:
            v = d[k]
            key = _yaml_scalar(k)
            if isinstance(v, (dict, list)) and v:
                lines.append(f"{spacer}{key}:")
                lines.append(dict_to_yaml(v, indent + 2))
            elif isinstance(v, (dict, list)):
                lines.append(f"{spacer}{key}: {'{}' if isinstance(v, dict) else '[]'}")
            else:
                lines.append(f"{spacer}{key}: {_yaml_scalar(v)}")
    elif isinstance(d, list):
        for item in d:
            if isinstance(item, (dict, list)) and item:
                lines.append(f"{spacer}- {dict_to_yaml(item, indent + 2).lstrip()}")
            else:
                lines.append(f"{spacer}- {_yaml_scalar(item) if not isinstance(item, (dict, list)) else ('{}' if isinstance(item, dict) else '[]')}")
    else:
        lines.append(f"{spacer}{_yaml_scalar(d)}")
    return "\n".join(lines)

def redaction_banner(hidden):
    return (f"# Lex redacted {hidden} secret-looking value{'s' if hidden != 1 else ''} "
            f"(env vars with credential-like names, and credentials embedded in URLs).\n") if hidden else ""

def load_demo_state():
    _, _, state_file = parse_cluster.get_file_paths("demo")
    if not os.path.exists(state_file):
        scrape_context("demo")
    with open(state_file, 'r', encoding='utf-8') as f:
        return json.load(f)

def load_demo_raw_items(kind):
    """Items from the demo fixture's raw-nodes.json / raw-pods.json."""
    nodes_file, pods_file, _ = parse_cluster.get_file_paths("demo")
    with open(nodes_file if kind == "nodes" else pods_file, 'r', encoding='utf-8') as f:
        return json.load(f).get("items", [])

# Compiled state files cached in memory (raw + gzipped bytes) keyed by path, invalidated by mtime
_state_cache = {}
_state_cache_lock = threading.Lock()

def load_state_bytes(path):
    """Returns (body, gzipped_body, content_hash, generated_at) for a compiled state file."""
    mtime = os.stat(path).st_mtime_ns
    with _state_cache_lock:
        cached = _state_cache.get(path)
        if cached and cached[0] == mtime:
            return cached[1]
    with open(path, 'rb') as f:
        body = f.read()
    data = json.loads(body)
    entry = (body, gzip.compress(body, 6), data.get("contentHash") or "", data.get("generatedAt"))
    with _state_cache_lock:
        _state_cache[path] = (mtime, entry)
    return entry

# Island summaries cached per state file, invalidated by mtime (compare() itself is cheap)
_island_cache = {}
_island_cache_lock = threading.Lock()
ARCHIPELAGO_CONFIG = os.environ.get("LEX_ARCHIPELAGO_CONFIG") or os.path.join(APP_DIR, "archipelago.json")
_archipelago_cfg_cache = (None, None, None)   # (mtime, config, error)

def _summary_for_path(path, demo_variant=False):
    """Summary of a compiled state file (None if missing). Cached until the file changes."""
    try:
        mtime = os.stat(path).st_mtime_ns
    except OSError:
        return None
    cache_key = (path, demo_variant)
    with _island_cache_lock:
        cached = _island_cache.get(cache_key)
        if cached and cached[0] == mtime:
            return cached[1]
    with open(path, 'r', encoding='utf-8') as f:
        state = json.load(f)
    summary = archipelago.summarize(archipelago.demo_variant(state) if demo_variant else state)
    with _island_cache_lock:
        _island_cache[cache_key] = (mtime, summary)
    return summary

def island_summary(ctx):
    """Compact summary of ctx's compiled state on disk, or None if it has never been compiled."""
    if ctx in ("demo", archipelago.DEMO_ISLAND):
        _, _, path = parse_cluster.get_file_paths("demo")
        if not os.path.exists(path):
            load_demo_state()
        return _summary_for_path(path, demo_variant=ctx == archipelago.DEMO_ISLAND)
    _, _, path = parse_cluster.get_file_paths(ctx)
    return _summary_for_path(path)

def fleet_standards(cfg):
    """Workloads in most of the clusters Lex has compiled (every per-context state file on disk, not just islands)."""
    summaries = []
    try:
        names = os.listdir(parse_cluster.DATA_DIR)
    except OSError:
        names = []
    for name in names:
        if name.startswith("cluster_state-") and name.endswith(".json"):
            try:
                summaries.append(_summary_for_path(os.path.join(parse_cluster.DATA_DIR, name)))
            except (OSError, ValueError):
                pass
    return archipelago.fleet_standards(summaries, cfg["fleetShare"])

def archipelago_config():
    """archipelago.json (optional, git-ignored): ignore lists, always-compare list, explicit pairs. Returns (cfg, error)."""
    global _archipelago_cfg_cache
    try:
        mtime = os.stat(ARCHIPELAGO_CONFIG).st_mtime_ns
    except OSError:
        return archipelago.load_config(None), None
    if _archipelago_cfg_cache[0] == mtime:
        return _archipelago_cfg_cache[1], _archipelago_cfg_cache[2]
    try:
        with open(ARCHIPELAGO_CONFIG, 'r', encoding='utf-8') as f:
            cfg, err = archipelago.load_config(json.load(f)), None
    except (OSError, ValueError) as e:
        cfg, err = archipelago.load_config(None), f"{os.path.basename(ARCHIPELAGO_CONFIG)}: {e}"
    _archipelago_cfg_cache = (mtime, cfg, err)
    return cfg, err

def island_payload(ctx, home_ctx, home, fleet, cfg):
    """One island for /api/v1/archipelago: its rooms, freshness and, for an environment sibling, which apps differ."""
    sync = get_sync_status(ctx) if ctx != archipelago.DEMO_ISLAND else {}
    ready = ctx == archipelago.DEMO_ISLAND or context_ready(ctx)
    entry = {"context": ctx, "ready": ready, "error": sync.get("lastError"),
             "freshAt": sync.get("freshAt") or sync.get("lastSuccessAt"), "visitable": ctx != archipelago.DEMO_ISLAND,
             "sibling": archipelago.are_siblings(home_ctx, ctx, cfg), "comparable": False, "notComparable": None}
    try:
        summary = island_summary(ctx)
    except (OSError, ValueError) as e:
        summary, entry["error"] = None, entry["error"] or f"Could not read compiled state: {e}"
    entry["island"] = {k: v for k, v in summary.items() if k != "workloads"} if summary else None
    if summary is None:
        entry["notComparable"] = "no state yet"
    elif not entry["sibling"]:
        entry["notComparable"] = "not an environment of the same system"
    elif not (home["hasWorkloadInfo"] and summary["hasWorkloadInfo"]):
        entry["notComparable"] = "a state without workload info (compiled by an older Lex); it fills in after the next sync"
    else:
        entry["comparable"] = True
        entry.update(archipelago.compare(home, summary, fleet, cfg))
    return entry

class LocalAPIServer(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        # Serve static assets from the app directory regardless of the process working directory
        super().__init__(*args, directory=APP_DIR, **kwargs)

    def end_headers(self):
        # No CORS headers: the UI is same-origin, and other sites must not be able to read responses.
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Referrer-Policy', 'no-referrer')
        if self.path.startswith('/api/v1/'):
            self.send_header('Cache-Control', 'no-store, no-cache, must-revalidate, max-age=0')
            self.send_header('X-Lex-Schema-Version', str(API_SCHEMA_VERSION))
        else:
            # Static assets must be revalidated on every load (cheap 304 via Last-Modified); otherwise browsers
            # heuristically cache index.html and keep running an old UI against a newer API after an upgrade.
            self.send_header('Cache-Control', 'no-cache')
        super().end_headers()

    def _allowed_hosts(self):
        port = self.server.server_address[1]
        return {f"{h}:{port}" for h in ALLOWED_HOSTNAMES}

    def check_host(self):
        """Rejects requests whose Host header isn't the loopback address (DNS rebinding defense)."""
        host = (self.headers.get('Host') or '').strip().lower()
        if host not in self._allowed_hosts():
            self.send_error_json(403, "Forbidden: invalid Host header")
            return False
        return True

    def check_post_origin(self):
        """CSRF defense for state-changing endpoints: JSON body and same-origin Origin header."""
        content_type = (self.headers.get('Content-Type') or '').split(';')[0].strip().lower()
        if content_type != 'application/json':
            self.send_error_json(415, "POST requests must use Content-Type: application/json")
            return False
        origin = self.headers.get('Origin')
        if origin is not None:
            parsed = urlparse(origin)
            if parsed.scheme != 'http' or parsed.netloc.lower() not in self._allowed_hosts():
                self.send_error_json(403, "Forbidden: cross-origin request")
                return False
        return True

    def serve_static(self, path, head_only=False):
        """Serves only allowlisted static assets; everything else 404s."""
        normalized = posixpath.normpath(unquote(path))
        if path.endswith('/') and normalized != '/':
            normalized += '/'
        if normalized in STATIC_FILES:
            self.path = STATIC_FILES[normalized]
        elif any(normalized.startswith(p) for p in STATIC_DIR_PREFIXES) and not normalized.endswith('/'):
            self.path = normalized
        else:
            self.send_error_json(404, "Not found")
            return
        if head_only:
            super().do_HEAD()
        else:
            super().do_GET()

    def do_HEAD(self):
        if not self.check_host():
            return
        self.serve_static(urlparse(self.path).path, head_only=True)

    def do_GET(self):
        if not self.check_host():
            return
        parsed_url = urlparse(self.path)
        path = parsed_url.path

        # Route: API state fetch
        if path == '/api/v1/state':
            self.handle_get_state()
        # Route: API contexts fetch
        elif path == '/api/v1/contexts':
            self.handle_get_contexts()
        # Route: API logs fetch
        elif path == '/api/v1/pods/logs':
            self.handle_pod_logs(parsed_url.query)
        # Route: API pod describe
        elif path == '/api/v1/pods/describe':
            self.handle_pod_describe(parsed_url.query)
        # Route: API pod spec
        elif path == '/api/v1/pods/spec':
            self.handle_pod_spec(parsed_url.query)
        elif path == '/api/v1/pods/diagnose':
            self.handle_pod_diagnose(parsed_url.query)
        # Route: API node spec
        elif path == '/api/v1/nodes/spec':
            self.handle_node_spec(parsed_url.query)
        # Route: API events fetch
        elif path == '/api/v1/events':
            self.handle_get_events(parsed_url.query)
        # Route: API DVR sessions list
        elif path == '/api/v1/dvr/sessions':
            self.handle_dvr_sessions()
        # Route: API DVR snapshots fetch
        elif path == '/api/v1/dvr/snapshots':
            self.handle_dvr_snapshots(parsed_url.query)
        elif path == '/api/v1/dvr/snapshot':
            self.handle_dvr_snapshot(parsed_url.query)
        elif path == '/api/v1/metrics/history':
            self.handle_metrics_history(parsed_url.query)
        elif path == '/api/v1/stream':
            self.handle_stream()
        elif path == '/api/v1/archipelago':
            self.handle_get_archipelago()
        elif path == '/api/v1/usage':
            self.handle_usage()
        elif path == '/api/v1/brand':
            self.handle_brand()
        elif path.startswith('/brand/'):
            self.serve_brand_asset(path)
        # Route: API DVR recording status
        elif path == '/api/v1/dvr/recording/status':
            self.handle_dvr_recording_status()
        else:
            self.serve_static(path)

    def do_POST(self):
        if not self.check_host() or not self.check_post_origin():
            return
        parsed_url = urlparse(self.path)
        path = parsed_url.path

        if path == '/api/v1/contexts/switch':
            self.handle_switch_context()
        elif path == '/api/v1/archipelago':
            self.handle_set_archipelago()
        elif path == '/api/v1/dvr/recording/toggle':
            self.handle_dvr_recording_toggle()
        elif path == '/api/v1/dvr/sessions/delete':
            self.handle_dvr_session_delete()
        else:
            self.send_error_json(404, "Endpoint not found")

    def handle_get_state(self):
        with active_context_lock:
            ctx = active_context
        _, _, state_file = parse_cluster.get_file_paths(ctx)

        if not os.path.exists(state_file):
            # A scrape just failed: report it instead of starting another (slow) kubectl attempt per poll
            status = get_sync_status(ctx)
            if status.get("lastError") and status.get("lastAttemptAt"):
                age = time.time() - calendar.timegm(time.strptime(status["lastAttemptAt"], "%Y-%m-%dT%H:%M:%SZ"))
                if age < SYNC_INTERVAL_SECONDS:
                    self.send_error_json(502, f"{status['lastError']} (retrying every {SYNC_INTERVAL_SECONDS}s)")
                    return
            print(f"State file '{state_file}' missing for context '{ctx}', generating synchronously...")
            if refresh_context(ctx) is None:
                self.send_error_json(502, get_sync_status(ctx).get("lastError") or f"Failed to generate cluster state for '{ctx}'")
                return

        if os.path.exists(state_file):
            try:
                body, body_gz, content_hash, generated_at = load_state_bytes(state_file)
            except Exception as e:
                self.send_error_json(500, f"Error reading state file: {str(e)}")
                return
            # Freshness metadata travels in headers so the body (and its ETag) stays identical across
            # scrapes of an unchanged cluster, letting polls be answered with 304 Not Modified.
            etag = f'"{parse_cluster.fingerprint([ctx, content_hash])}"'
            sync = get_sync_status(ctx)
            meta_headers = {
                'ETag': etag,
                'X-Lex-Context': json.dumps(ctx),
                'X-Lex-Generated-At': generated_at or '',
                'X-Lex-Sync': json.dumps(sync),
                'X-Lex-Sync-Interval': str(SYNC_INTERVAL_SECONDS),
                # Watch mode only recompiles on change: "fresh at" says the data was still current then
                'X-Lex-Fresh-At': sync.get("freshAt") or '',
            }
            if content_hash and etag in [t.strip() for t in (self.headers.get('If-None-Match') or '').split(',')]:
                self.send_response(304)
                for k, v in meta_headers.items():
                    self.send_header(k, v)
                self.end_headers()
                return
            self.send_bytes_response(200, body, 'application/json; charset=utf-8', body_gz, meta_headers)
        else:
            self.send_error_json(404, "Cluster state file could not be found.")

    def handle_brand(self):
        """The optional user-supplied brand (brand/brand.json): name, tagline, accent color and asset URLs."""
        info = brand.load()
        self.send_json_response(200, {"brand": info["brand"], "problems": info["problems"]})

    def serve_brand_asset(self, path):
        """Only files the current brand.json names are served. SVGs can carry scripts, so the response is sandboxed
        (it only ever renders as an <img>, where scripts don't run anyway)."""
        name = unquote(path[len('/brand/'):])
        found = brand.asset(name) if '/' not in name and '\\' not in name else None
        if not found:
            self.send_error_json(404, "Not found")
            return
        data, content_type = found
        self.send_response(200)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Content-Security-Policy', "default-src 'none'; style-src 'unsafe-inline'; img-src data:; sandbox")
        self.end_headers()
        self.wfile.write(data)

    def handle_get_archipelago(self):
        """F-36: the active cluster plus its islands; sibling islands say which apps run a different version."""
        with active_context_lock:
            home_ctx = active_context
        try:
            home = island_summary(home_ctx)
            if home is None:
                raise OSError("not compiled yet")
        except (OSError, ValueError) as e:
            self.send_error_json(503, f"No state for '{home_ctx}' yet: {e}")
            return
        cfg, cfg_error = archipelago_config()
        fleet = fleet_standards(cfg)
        members = [c for c in archipelago_contexts() if c != home_ctx]
        warm = sorted(c for c in wanted_contexts() if c != home_ctx and context_ready(c))
        self.send_json_response(200, {
            "home": home_ctx,
            "homeHasWorkloadInfo": home["hasWorkloadInfo"],
            "members": members,
            "max": archipelago.MAX_ISLANDS,
            "suggested": archipelago.suggest(home_ctx, list_kube_contexts(), warm, cfg),
            "fleetSize": len(fleet),
            "hiddenReasons": archipelago.HIDDEN_REASONS,
            "configError": cfg_error,
            "islands": [island_payload(ctx, home_ctx, home, fleet, cfg) for ctx in members],
        })

    def handle_set_archipelago(self):
        """Sets the island contexts (max 4). They join the warm set, so they are kept current like O-05 contexts."""
        try:
            data = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))).decode('utf-8'))
            requested = data.get("contexts")
        except Exception as e:
            self.send_error_json(400, f"Invalid JSON payload: {e}")
            return
        if not isinstance(requested, list) or not all(isinstance(c, str) for c in requested):
            self.send_error_json(400, "'contexts' must be a list of context names")
            return
        known = None
        chosen = []
        for c in requested:
            if c in chosen:
                continue
            if c != archipelago.DEMO_ISLAND:
                if c == "demo" or not is_valid_context_name(c):
                    self.send_error_json(400, f"Invalid context name: {c!r}")
                    return
                known = known if known is not None else set(list_kube_contexts())
                if c not in known:
                    self.send_error_json(404, f"Context '{c}' not found in kubeconfig")
                    return
            chosen.append(c)
        if len(chosen) > archipelago.MAX_ISLANDS:
            self.send_error_json(400, f"At most {archipelago.MAX_ISLANDS} islands")
            return
        global archipelago_members
        with archipelago_lock:
            archipelago_members = chosen
        if WATCH_ENABLED:
            stop_unneeded_watchers()
        # Start warming new islands now rather than at the next background interval
        cold = [c for c in chosen if c != archipelago.DEMO_ISLAND and not context_ready(c)]
        if cold:
            threading.Thread(target=lambda: [ensure_watcher(c) if WATCH_ENABLED else scrape_context(c) for c in cold],
                             daemon=True).start()
        self.send_json_response(200, {"members": chosen})

    def handle_usage(self):
        """Actual CPU/memory usage (metrics-server) for the rightsizing lens, with recent peaks."""
        with active_context_lock:
            ctx = active_context
        if ctx == "demo":
            try:
                data = usage.synthetic_usage(load_demo_state())
            except Exception as e:
                self.send_error_json(500, f"Could not build demo usage: {e}")
                return
        else:
            tracker = usage.tracker_for(ctx)
            if tracker.sampled_at is None or tracker.due(SYNC_INTERVAL_SECONDS):
                tracker.sample()   # first request for this context (or a stale sample): read it now
            data = tracker.snapshot()
        data["context"] = ctx
        self.send_json_response(200, data)

    def handle_stream(self):
        """Server-sent events: `event: state` whenever the active context's state changes (watch mode),
        plus a heartbeat comment so proxies and the browser keep the connection open."""
        self.close_connection = True
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream; charset=utf-8')
        self.end_headers()
        with state_change:
            seen = state_version
        try:
            hello = {"watch": WATCH_ENABLED, "version": seen}
            self.wfile.write(f"retry: 5000\nevent: hello\ndata: {json.dumps(hello)}\n\n".encode('utf-8'))
            self.wfile.flush()
            while True:
                with state_change:
                    state_change.wait_for(lambda: state_version != seen, timeout=STREAM_HEARTBEAT_SECONDS)
                    current = state_version
                if current != seen:
                    seen = current
                    with active_context_lock:
                        ctx = active_context
                    payload = json.dumps({"version": current, "context": ctx})
                    self.wfile.write(f"event: state\ndata: {payload}\n\n".encode('utf-8'))
                else:
                    self.wfile.write(b": ping\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass   # browser went away

    def handle_get_contexts(self):
        try:
            contexts = list_kube_contexts()

            if "demo" not in contexts:
                contexts = ["demo"] + contexts
                
            with active_context_lock:
                ctx = active_context
            wanted = wanted_contexts()
            self.send_json_response(200, {
                "contexts": contexts,
                "activeContext": ctx,
                # O-05: contexts with current state in the background (switching to them is instant)
                "warm": sorted(c for c in wanted if c != ctx and context_ready(c)),
                "warming": sorted(c for c in wanted if c != ctx and not context_ready(c)),
                "recording": sorted(recordings()),
            })
        except Exception as e:
            self.send_json_response(200, {
                "contexts": ["demo"],
                "activeContext": "demo",
                "warning": f"Could not query host contexts: {str(e)}"
            })

    def handle_switch_context(self):
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            post_data = self.rfile.read(content_length)
            data = json.loads(post_data.decode('utf-8'))
        except Exception as e:
            self.send_error_json(400, f"Invalid JSON payload: {str(e)}")
            return

        target_context = data.get("context")
        if not target_context:
            self.send_error_json(400, "Missing 'context' field in payload")
            return

        if not isinstance(target_context, str) or (target_context != "demo" and not is_valid_context_name(target_context)):
            self.send_error_json(400, "Invalid context name format")
            return

        if target_context != "demo" and target_context not in list_kube_contexts():
            self.send_error_json(404, f"Context '{target_context}' not found in kubeconfig")
            return

        global active_context
        with active_context_lock:
            previous = active_context
        instant = context_ready(target_context) and target_context in wanted_contexts()
        if refresh_context(target_context) is None:
            error = get_sync_status(target_context).get("lastError") or "unknown error"
            self.send_error_json(502, f"Failed to switch to '{target_context}': {error}")
            return
        with active_context_lock:
            active_context = target_context
        remember_recent(previous, target_context)
        if WATCH_ENABLED:
            stop_unneeded_watchers()
        notify_state_change()
        self.send_json_response(200, {"status": "success", "activeContext": target_context, "instant": instant})

    def handle_pod_logs(self, query_string):
        params = parse_qs(query_string)
        pod = params.get('pod', [None])[0]
        namespace = params.get('namespace', [None])[0]
        container = params.get('container', [None])[0]
        previous = params.get('previous', [None])[0] == '1'

        if not pod or not namespace:
            self.send_error_json(400, "Missing required query parameters: 'pod' and 'namespace'")
            return

        if not is_valid_object_name(pod) or not is_valid_namespace(namespace):
            self.send_error_json(400, "Invalid characters in pod or namespace parameter")
            return

        # Container names are DNS-1123 labels, like namespaces
        if container is not None and not is_valid_namespace(container):
            self.send_error_json(400, "Invalid characters in container parameter")
            return

        with active_context_lock:
            ctx = active_context

        if ctx == "demo":
            if previous:
                self.send_text_response(200, diagnose.demo_previous_logs(pod, container))
                return
            mock_logs = f"""[DEMO MODE ACTIVE] - Streaming logs for pod '{pod}' in namespace '{namespace}'
2026-05-22T10:00:00Z INFO [app] Initializing container...
2026-05-22T10:00:02Z INFO [app] Database connection pool established (10 connections).
2026-05-22T10:00:05Z INFO [app] Server listening on :8080
2026-05-22T10:05:00Z WARN [app] Request latency spike detected (duration: 350ms)
2026-05-22T10:15:32Z INFO [app] Garbage collection completed (released 42MB)
2026-05-22T10:45:12Z DEBUG [app] Active websocket connections: 24"""
            self.send_text_response(200, mock_logs)
            return

        try:
            cmd = ["kubectl", f"--context={ctx}", "logs", pod, "-n", namespace, "--tail=200"]
            if container:
                cmd.append(f"--container={container}")
            if previous:
                cmd.append("--previous")
            print(f"Running secure command: {' '.join(cmd)}")
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            if res.returncode == 0:
                self.send_text_response(200, res.stdout)
            else:
                error_msg = res.stderr or "Unknown error fetching logs"
                self.send_text_response(500, f"▲ kubectl failed:\n{error_msg}")
        except subprocess.TimeoutExpired:
            self.send_text_response(504, "▲ Command timeout expired while connecting to cluster.")
        except Exception as e:
            self.send_text_response(500, f"▲ Server error executing kubectl logs: {str(e)}")

    def handle_pod_describe(self, query_string):
        params = parse_qs(query_string)
        pod = params.get('pod', [None])[0]
        namespace = params.get('namespace', [None])[0]

        if not pod or not namespace:
            self.send_error_json(400, "Missing required query parameters: 'pod' and 'namespace'")
            return

        if not is_valid_object_name(pod) or not is_valid_namespace(namespace):
            self.send_error_json(400, "Invalid characters in pod or namespace parameter")
            return

        with active_context_lock:
            ctx = active_context

        if ctx == "demo":
            mock_describe = f"""Name:         {pod}
Namespace:    {namespace}
Priority:     0
Node:         node-alpha/192.168.1.100
Start Time:   Fri, 22 May 2026 10:00:00 -0400
Labels:       app=demo-app
              pod-template-hash=7d4f9b8c
Annotations:  kubernetes.io/config.seen: 2026-05-22T10:00:00Z
Status:       Running
IP:           10.244.1.42
Containers:
  demo-container:
    Container ID:   containerd://abc123xyz
    Image:          nginx:stable-alpine
    Port:           80/TCP
    State:          Running
      Started:      Fri, 22 May 2026 10:00:02 -0400
    Ready:          True
    Restart Count:  0
    Requests:
      cpu:        100m
      memory:     12Gi
Conditions:
  Type              Status
  Initialized       True 
  Ready             True 
  ContainersReady   True 
  PodScheduled      True 
Events:
  Type    Reason     Age   From               Message
  ----    ------     ----  ----               -------
  Normal  Scheduled  15m   default-scheduler  Successfully assigned {namespace}/{pod} to node-alpha
  Normal  Pulling    15m   kubelet            Pulling image "nginx:stable-alpine"
  Normal  Pulled     14m   kubelet            Successfully pulled image "nginx:stable-alpine" in 2.3s
  Normal  Created    14m   kubelet            Created container demo-container
  Normal  Started    14m   kubelet            Started container demo-container"""
            self.send_text_response(200, mock_describe)
            return

        try:
            print(f"Running secure command: kubectl describe pod {pod} -n {namespace} (context: {ctx})")
            cmd = ["kubectl", f"--context={ctx}", "describe", "pod", pod, "-n", namespace]
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            if res.returncode == 0:
                text, hidden = redaction.redact_describe_text(res.stdout)
                self.send_text_response(200, redaction_banner(hidden) + text)
            else:
                error_msg = res.stderr or "Unknown error describing pod"
                self.send_text_response(500, f"▲ kubectl failed:\n{error_msg}")
        except subprocess.TimeoutExpired:
            self.send_text_response(504, "▲ Command timeout expired while connecting to cluster.")
        except Exception as e:
            self.send_text_response(500, f"▲ Server error executing kubectl describe: {str(e)}")

    def handle_pod_spec(self, query_string):
        params = parse_qs(query_string)
        pod = params.get('pod', [None])[0]
        namespace = params.get('namespace', [None])[0]

        if not pod or not namespace:
            self.send_error_json(400, "Missing required query parameters: 'pod' and 'namespace'")
            return

        if not is_valid_object_name(pod) or not is_valid_namespace(namespace):
            self.send_error_json(400, "Invalid characters in pod or namespace parameter")
            return

        with active_context_lock:
            ctx = active_context

        if ctx == "demo":
            try:
                found_pod = next((p for p in load_demo_raw_items("pods")
                                  if (p.get("metadata") or {}).get("name") == pod
                                  and (p.get("metadata") or {}).get("namespace", "default") == namespace), None)
                if found_pod:
                    hidden = redaction.sanitize_pod_for_display(found_pod)
                    self.send_text_response(200, redaction_banner(hidden) + dict_to_yaml(found_pod))
                    return
            except Exception as e:
                print(f"Error serving mock pod spec: {e}")

            mock_spec = f"""apiVersion: v1
kind: Pod
metadata:
  name: {pod}
  namespace: {namespace}
  labels:
    app: demo-app
spec:
  containers:
  - name: main
    image: nginx:stable-alpine
    resources:
      requests:
        memory: 12Gi
        cpu: 500m"""
            self.send_text_response(200, mock_spec)
            return

        try:
            print(f"Running secure command: kubectl get pod {pod} -n {namespace} -o yaml (context: {ctx})")
            cmd = ["kubectl", f"--context={ctx}", "get", "pod", pod, "-n", namespace, "-o", "json"]
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            if res.returncode == 0:
                pod_obj = json.loads(res.stdout)
                hidden = redaction.sanitize_pod_for_display(pod_obj)
                self.send_text_response(200, redaction_banner(hidden) + dict_to_yaml(pod_obj))
            else:
                error_msg = res.stderr or "Unknown error fetching pod spec"
                self.send_text_response(500, f"▲ kubectl failed:\n{error_msg}")
        except subprocess.TimeoutExpired:
            self.send_text_response(504, "▲ Command timeout expired while connecting to cluster.")
        except Exception as e:
            self.send_text_response(500, f"▲ Server error executing kubectl get pod: {str(e)}")

    def handle_pod_diagnose(self, query_string):
        """F-24: a plain-English diagnosis built from the live pod, its events, its node and recent usage."""
        params = parse_qs(query_string)
        pod = params.get('pod', [None])[0]
        namespace = params.get('namespace', [None])[0]

        if not pod or not namespace:
            self.send_error_json(400, "Missing required query parameters: 'pod' and 'namespace'")
            return

        if not is_valid_object_name(pod) or not is_valid_namespace(namespace):
            self.send_error_json(400, "Invalid characters in pod or namespace parameter")
            return

        with active_context_lock:
            ctx = active_context

        events_error = None
        if ctx == "demo":
            pod_obj = next((p for p in load_demo_raw_items("pods")
                            if (p.get("metadata") or {}).get("name") == pod
                            and (p.get("metadata") or {}).get("namespace", "default") == namespace), None)
            if pod_obj is None:
                self.send_error_json(404, f"Pod {namespace}/{pod} was not found")
                return
            events = diagnose.synthesize_demo_events(pod_obj)
        else:
            def run(cmd):
                return subprocess.run(cmd, capture_output=True, text=True, timeout=15)
            base = ["kubectl", f"--context={ctx}", "--request-timeout=10s"]
            print(f"Running secure command: kubectl get pod {pod} -n {namespace} -o json + events (context: {ctx})")
            with ThreadPoolExecutor(max_workers=2) as pool:
                pod_future = pool.submit(run, base + ["get", "pod", pod, "-n", namespace, "-o", "json"])
                events_future = pool.submit(run, base + ["get", "events", "-n", namespace, "-o", "json",
                                                         "--field-selector", f"involvedObject.name={pod},involvedObject.kind=Pod"])
                try:
                    pod_res = pod_future.result()
                except subprocess.TimeoutExpired:
                    self.send_error_json(504, "Timed out fetching the pod from the cluster")
                    return
                except Exception as e:
                    self.send_error_json(500, f"Could not run kubectl: {e}")
                    return
                try:
                    events_res = events_future.result()
                except Exception as e:
                    events_res, events_error = None, f"Could not fetch events: {e}"
            if pod_res.returncode != 0:
                err = (pod_res.stderr or "kubectl failed").strip()
                self.send_error_json(404 if "NotFound" in err else 502, err[:500])
                return
            try:
                pod_obj = json.loads(pod_res.stdout)
            except ValueError:
                self.send_error_json(502, "kubectl returned invalid JSON for the pod")
                return
            events = None
            if events_res is not None:
                if events_res.returncode == 0:
                    try:
                        events = json.loads(events_res.stdout)
                    except ValueError:
                        events_error = "kubectl returned invalid JSON for events"
                else:
                    events_error = (events_res.stderr or "kubectl get events failed").strip()[:300]

        # The node's conditions come from the compiled state (no extra kubectl call)
        node = None
        node_name = (pod_obj.get("spec") or {}).get("nodeName")
        if node_name:
            try:
                state = load_demo_state() if ctx == "demo" else read_state_file(ctx)
                node = next((n for n in state.get("nodes") or [] if n.get("name") == node_name), None)
            except (OSError, ValueError):
                pass

        # Recent usage, only if the rightsizing sampler already has it (never triggers a metrics read)
        pod_usage = None
        try:
            snap = usage.synthetic_usage(load_demo_state()) if ctx == "demo" else usage.tracker_for(ctx).snapshot()
            row = (snap.get("pods") or {}).get(f"{namespace}/{pod}") if snap.get("available") else None
            if row:
                pod_usage = {"cpu": row[0], "memoryBytes": row[1] * 1024 ** 3, "peakCpu": row[2],
                             "peakMemoryBytes": row[3] * 1024 ** 3, "windowMinutes": snap.get("windowMinutes")}
        except Exception as e:
            print(f"▲ Usage unavailable for diagnosis: {e}")

        redaction.sanitize_pod_for_display(pod_obj)
        try:
            result = diagnose.diagnose(pod_obj, events, node=node, usage=pod_usage)
        except Exception as e:
            self.send_error_json(500, f"Diagnosis failed: {e}")
            return
        result["context"] = ctx
        result["eventsError"] = events_error
        self.send_json_response(200, result)

    def handle_node_spec(self, query_string):
        params = parse_qs(query_string)
        node_name = params.get('node', [None])[0]

        if not node_name:
            self.send_error_json(400, "Missing required query parameter: 'node'")
            return

        if not is_valid_object_name(node_name):
            self.send_error_json(400, "Invalid characters in node parameter")
            return

        with active_context_lock:
            ctx = active_context

        if ctx == "demo":
            try:
                found_node = next((n for n in load_demo_raw_items("nodes")
                                   if (n.get("metadata") or {}).get("name") == node_name), None)
                if found_node:
                    yaml_text = dict_to_yaml(found_node)
                    self.send_text_response(200, yaml_text)
                    return
            except Exception as e:
                print(f"Error serving mock node spec: {e}")

            mock_spec = f"""apiVersion: v1
kind: Node
metadata:
  name: {node_name}
  labels:
    kubernetes.io/hostname: {node_name}
    kubernetes.io/os: linux
spec:
  providerID: aws:///us-east-1a/i-0a1b2c3d4e5f60000
status:
  capacity:
    cpu: "8"
    memory: 24Gi
  allocatable:
    cpu: "8"
    memory: 24Gi"""
            self.send_text_response(200, mock_spec)
            return

        try:
            print(f"Running secure command: kubectl get node {node_name} -o yaml (context: {ctx})")
            cmd = ["kubectl", f"--context={ctx}", "get", "node", node_name, "-o", "yaml"]
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            if res.returncode == 0:
                self.send_text_response(200, res.stdout)
            else:
                error_msg = res.stderr or "Unknown error fetching node spec"
                self.send_text_response(500, f"▲ kubectl failed:\n{error_msg}")
        except subprocess.TimeoutExpired:
            self.send_text_response(504, "▲ Command timeout expired while connecting to cluster.")
        except Exception as e:
            self.send_text_response(500, f"▲ Server error executing kubectl get node: {str(e)}")

    def handle_get_events(self, query_string):
        params = parse_qs(query_string)
        name = params.get('name', [None])[0]
        kind = params.get('kind', [None])[0]
        namespace = params.get('namespace', [None])[0]

        if not name or not kind:
            self.send_error_json(400, "Missing required query parameters: 'name' and 'kind'")
            return

        if not is_valid_object_name(name) or (namespace and not is_valid_namespace(namespace)):
            self.send_error_json(400, "Invalid characters in name or namespace parameter")
            return

        kind = kind.lower()
        if kind not in ['pod', 'node']:
            self.send_error_json(400, "Invalid kind. Must be 'pod' or 'node'")
            return

        with active_context_lock:
            ctx = active_context

        if ctx == "demo":
            mock_events = f"""LAST SEEN   TYPE     REASON      OBJECT    MESSAGE
12m         Normal   Scheduled   pod/{name}   Successfully assigned {namespace or 'default'}/{name} to node
12m         Normal   Pulling     pod/{name}   Pulling image "nginx:stable-alpine"
11m         Normal   Pulled      pod/{name}   Successfully pulled image "nginx:stable-alpine" in 1.4s
11m         Normal   Created     pod/{name}   Created container
11m         Normal   Started     pod/{name}   Started container"""
            self.send_text_response(200, mock_events)
            return

        try:
            cmd = ["kubectl"]
            cmd += [f"--context={ctx}"]
            if kind == 'pod':
                ns = namespace if namespace else 'default'
                cmd += ["get", "events", "-n", ns, "--field-selector", f"involvedObject.name={name}"]
            else: # node
                cmd += ["get", "events", "--all-namespaces", "--field-selector", f"involvedObject.name={name}"]

            print(f"Running secure command: {' '.join(cmd)}")
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            if res.returncode == 0:
                self.send_text_response(200, res.stdout or "No events found for this object.")
            else:
                error_msg = res.stderr or "Unknown error fetching events"
                self.send_text_response(500, f"▲ kubectl failed:\n{error_msg}")
        except subprocess.TimeoutExpired:
            self.send_text_response(504, "▲ Command timeout expired while connecting to cluster.")
        except Exception as e:
            self.send_text_response(500, f"▲ Server error executing kubectl events: {str(e)}")

    def handle_dvr_sessions(self):
        try:
            sessions = dvr_db.get_sessions()
            self.send_json_response(200, {"sessions": sessions})
        except Exception as e:
            self.send_error_json(500, f"Error listing sessions: {str(e)}")

    def handle_dvr_snapshots(self, query_string):
        params = parse_qs(query_string)
        session_id = params.get('session_id', [None])[0]
        if not session_id:
            self.send_error_json(400, "Missing required query parameter: 'session_id'")
            return
            
        if not re.match(r"^[a-zA-Z0-9_./:@-]+$", session_id):
            self.send_error_json(400, "Invalid session_id format")
            return
            
        try:
            # Timeline only (ids, timestamps, incident summaries); frames are fetched individually
            self.send_json_response(200, {"frames": dvr_db.get_timeline(session_id)})
        except Exception as e:
            self.send_error_json(500, f"Error fetching snapshots: {str(e)}")

    METRICS_WINDOWS = {"1h": 3600, "6h": 6 * 3600, "24h": 24 * 3600, "7d": 7 * 24 * 3600}

    def handle_metrics_history(self, query_string):
        params = parse_qs(query_string)
        window = params.get('window', ['1h'])[0]
        if window not in self.METRICS_WINDOWS:
            self.send_error_json(400, f"window must be one of {', '.join(self.METRICS_WINDOWS)}")
            return
        with active_context_lock:
            ctx = params.get('context', [active_context])[0]
        if ctx != "demo" and not is_valid_context_name(ctx):
            self.send_error_json(400, "Invalid context name format")
            return
        end = None
        if params.get('end'):
            try:
                end = datetime.datetime.fromisoformat(params['end'][0].replace('Z', '+00:00'))
            except ValueError:
                self.send_error_json(400, "end must be an ISO-8601 timestamp")
                return
        try:
            if ctx == "demo":
                points = metrics.synthetic_history(load_demo_state(), self.METRICS_WINDOWS[window], end)
                synthetic = True
            else:
                points = metrics.history(ctx, self.METRICS_WINDOWS[window], end)
                synthetic = False
        except Exception as e:
            self.send_error_json(500, f"Error reading metrics history: {e}")
            return
        self.send_json_response(200, {"context": ctx, "window": window, "synthetic": synthetic, "points": points})

    def handle_dvr_snapshot(self, query_string):
        params = parse_qs(query_string)
        session_id = params.get('session_id', [None])[0]
        snapshot_id = params.get('id', [None])[0]
        if not session_id or not snapshot_id or not snapshot_id.isdigit():
            self.send_error_json(400, "Required query parameters: 'session_id' and numeric 'id'")
            return
        if not re.match(r"^[a-zA-Z0-9_./:@-]+$", session_id):
            self.send_error_json(400, "Invalid session_id format")
            return
        snapshot = dvr_db.get_snapshot(session_id, int(snapshot_id))
        if snapshot is None:
            self.send_error_json(404, "Snapshot not found")
            return
        self.send_json_response(200, snapshot)

    def recording_payload(self, ctx):
        recs = recordings()
        return {
            # The fields the UI had before O-05 describe the context on screen
            "recording": ctx in recs,
            "session_id": recs.get(ctx),
            "cluster_name": ctx if ctx in recs else None,
            "recordings": [{"context": c, "session_id": sid} for c, sid in sorted(recs.items())],
        }

    def handle_dvr_recording_status(self):
        with active_context_lock:
            ctx = active_context
        self.send_json_response(200, self.recording_payload(ctx))

    def handle_dvr_recording_toggle(self):
        """Starts or stops recording one context (the one on screen unless the payload names another)."""
        try:
            length = int(self.headers.get('Content-Length', 0))
            data = json.loads(self.rfile.read(length).decode('utf-8') or "{}") if length else {}
        except Exception as e:
            self.send_error_json(400, f"Invalid JSON payload: {str(e)}")
            return
        with active_context_lock:
            ctx = active_context
        target = data.get("context") or ctx
        if not isinstance(target, str) or (target != "demo" and not is_valid_context_name(target)):
            self.send_error_json(400, "Invalid context name format")
            return
        if target != "demo" and target != ctx and target not in list_kube_contexts():
            self.send_error_json(404, f"Context '{target}' not found in kubeconfig")
            return

        with active_recordings_lock:
            stopping_session = active_recordings.pop(target, None)
            session_id = None
            if stopping_session is None:
                session_id = dvr_db.start_session(target)
                if not session_id:
                    self.send_error_json(500, "Could not start recording session")
                    return
                active_recordings[target] = session_id

        if stopping_session is not None:
            dvr_db.end_session(stopping_session)
            if WATCH_ENABLED:
                stop_unneeded_watchers()
            payload = self.recording_payload(ctx)
            payload.update({"status": "success", "stopped": {"context": target, "session_id": stopping_session}})
            self.send_json_response(200, payload)
            return

        # Record an initial snapshot immediately (outside the lock so the background sync isn't blocked)
        state = refresh_context(target)
        if state is not None:
            dvr_db.add_snapshot(session_id, target, state)
        payload = self.recording_payload(ctx)
        payload.update({"status": "success", "started": {"context": target, "session_id": session_id}})
        self.send_json_response(200, payload)

    def handle_dvr_session_delete(self):
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            post_data = self.rfile.read(content_length)
            data = json.loads(post_data.decode('utf-8'))
        except Exception as e:
            self.send_error_json(400, f"Invalid JSON payload: {str(e)}")
            return

        session_id = data.get("session_id")
        if not session_id:
            self.send_error_json(400, "Missing 'session_id' field in payload")
            return

        if not re.match(r"^[a-zA-Z0-9_./:@-]+$", session_id):
            self.send_error_json(400, "Invalid session_id format")
            return

        with active_recordings_lock:
            # If the session to delete is still recording, stop it first
            for c, sid in list(active_recordings.items()):
                if sid == session_id:
                    del active_recordings[c]

        try:
            success = dvr_db.delete_session(session_id)
            if success:
                self.send_json_response(200, {"status": "success"})
            else:
                self.send_error_json(500, "Failed to delete session")
        except Exception as e:
            self.send_error_json(500, f"Error deleting session: {str(e)}")

    def accepts_gzip(self):
        return 'gzip' in (self.headers.get('Accept-Encoding') or '').lower()

    def send_bytes_response(self, code, body, content_type, body_gz=None, extra_headers=None):
        """Sends body, gzip-encoded when the client accepts it and it's worth compressing."""
        use_gzip = self.accepts_gzip() and len(body) >= GZIP_MIN_BYTES
        if use_gzip and body_gz is None:
            body_gz = gzip.compress(body, 6)
        payload = body_gz if use_gzip else body
        self.send_response(code)
        self.send_header('Content-Type', content_type)
        if use_gzip:
            self.send_header('Content-Encoding', 'gzip')
        self.send_header('Vary', 'Accept-Encoding')
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def send_json_response(self, code, data):
        payload = json.dumps(data, separators=(",", ":")).encode('utf-8')
        self.send_bytes_response(code, payload, 'application/json; charset=utf-8')

    def send_text_response(self, code, text):
        self.send_response(code)
        self.send_header('Content-Type', 'text/plain; charset=utf-8')
        payload = text.encode('utf-8')
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def send_error_json(self, code, message):
        self.send_json_response(code, {"error": message})

def run_bg_sync(interval=SYNC_INTERVAL_SECONDS):
    """Background thread: keeps warm contexts current, feeds the vitals history, and records DVR frames."""
    print(f"Background Kubernetes context poller started ({interval}s interval).")
    last_prune = 0.0
    next_run = time.time() + interval  # main() has just compiled fresh state at boot
    while True:
        time.sleep(max(1.0, next_run - time.time()))
        next_run = max(next_run + interval, time.time())
        started = time.time()
        if started - last_prune > 3600:
            last_prune = started
            try:
                dvr_db.prune_old_snapshots(DVR_RETENTION_DAYS)
                metrics.prune()
            except Exception as e:
                print(f"▲ Retention pass failed: {e}")
        try:
            with active_context_lock:
                ctx = active_context
            if ctx != "demo":
                try:
                    usage.sample_if_due(ctx, interval)   # keeps the rightsizing peaks current (one small call)
                except Exception as e:
                    print(f"▲ Usage sample failed for '{ctx}': {e}")
            if WATCH_ENABLED:
                record_from_watchers()
            else:
                scrape_and_record()
        except Exception as e:
            print(f"Error in background sync worker: {e}")

def scrape_and_record():
    """Polling mode: scrape every warm context in parallel (O-05), then add DVR frames for the recorded ones."""
    recs = recordings()
    contexts = sorted(wanted_contexts() | ({"demo"} if "demo" in recs else set()))
    states = {}
    if contexts:
        with ThreadPoolExecutor(max_workers=min(4, len(contexts))) as pool:
            for c, state in zip(contexts, pool.map(scrape_context, contexts)):
                states[c] = state
    for c, session_id in recs.items():
        if states.get(c) is not None:
            dvr_db.add_snapshot(session_id, c, states[c])

def record_from_watchers():
    """Watch mode: watchers keep each warm context's state file current, so each interval just samples them for
    the vitals history and the DVR (and starts / stops watchers as the warm set changes)."""
    stop_unneeded_watchers()
    for c in wanted_contexts():
        w = ensure_watcher(c)
        if w.first_compile.is_set():
            try:
                metrics.record(c, read_state_file(c))
            except Exception as e:
                print(f"▲ Could not record metrics for '{c}': {e}")
    for c, session_id in recordings().items():
        if c == "demo":
            state = scrape_context("demo")
        else:
            w = ensure_watcher(c)
            state = read_state_file(c) if w.first_compile.is_set() else None
        if state is not None:
            dvr_db.add_snapshot(session_id, c, state)

def report_stale_raw_dumps():
    """O-02: per-context raw dumps are no longer written or read. Point at leftovers instead of deleting user files."""
    if parse_cluster.KEEP_RAW_DUMPS:
        return
    try:
        stale = [n for n in os.listdir(parse_cluster.DATA_DIR)
                 if re.match(r"^raw-(nodes|pods|workloads)-.+\.json$", n)]
    except OSError:
        return
    if stale:
        size = sum(os.path.getsize(os.path.join(parse_cluster.DATA_DIR, n)) for n in stale) / 1e6
        print(f"● {len(stale)} raw cluster dump(s) from earlier versions ({size:.0f} MB) in {parse_cluster.DATA_DIR} are no longer "
              f"used. Lex now keeps raw data in memory only. Remove them with: rm {parse_cluster.DATA_DIR}/raw-*-*.json")

def init_active_context():
    """Attempts to discover active kubectl context at boot."""
    global active_context
    try:
        res = subprocess.run(["kubectl", "config", "current-context"], capture_output=True, text=True, timeout=2)
        if res.returncode == 0 and res.stdout.strip():
            ctx = res.stdout.strip()
            with active_context_lock:
                active_context = ctx
            print(f"Discovered active kubectl context on boot: {ctx}")
        else:
            with active_context_lock:
                active_context = "demo"
            print("No active kubectl context found on boot. Defaulting to 'demo' context.")
    except Exception as e:
        with active_context_lock:
            active_context = "demo"
        print(f"Failed to query active context on boot: {e}. Defaulting to 'demo' context.")

def main():
    global active_context, SYNC_INTERVAL_SECONDS, DVR_RETENTION_DAYS, WATCH_ENABLED
    import argparse
    parser = argparse.ArgumentParser(description="Lex local API server")
    parser.add_argument("--port", type=int, default=PORT, help=f"Port to listen on (default: {PORT})")
    parser.add_argument("--interval", type=int, default=SYNC_INTERVAL_SECONDS,
                        help=f"Seconds between background cluster scrapes (default: {SYNC_INTERVAL_SECONDS})")
    parser.add_argument("--dvr-retention-days", type=float, default=DVR_RETENTION_DAYS,
                        help=f"Delete DVR frames older than this many days; 0 keeps everything (default: {DVR_RETENTION_DAYS})")
    parser.add_argument("--no-watch", action="store_true",
                        help="Re-list the whole cluster every interval instead of streaming changes with Kubernetes watches")
    parser.add_argument("--warm", default="",
                        help="Comma-separated contexts to keep current in the background for instant switching, or 'all'")
    parser.add_argument("--warm-recent", type=int, default=None,
                        help="Also keep this many recently used contexts warm (default: 2 with watches, 0 with --no-watch)")
    parser.add_argument("--brand", default=None,
                        help="Directory with a brand.json and its logo files (default: brand/ next to server.py, or LEX_BRAND_DIR)")
    parser.add_argument("--keep-raw-dumps", action="store_true",
                        help="Also write sanitized raw kubectl lists to data/ (debugging); by default only compiled state is stored")
    args = parser.parse_args()
    DVR_RETENTION_DAYS = args.dvr_retention_days
    WATCH_ENABLED = not args.no_watch
    global WARM_RECENT, WARM_CONTEXTS
    # Polling re-lists whole clusters every interval, so it only warms what was asked for
    WARM_RECENT = max(0, args.warm_recent if args.warm_recent is not None else (2 if WATCH_ENABLED else 0))
    if args.warm.strip() == "all":
        WARM_CONTEXTS = set(list_kube_contexts())
    else:
        requested = {c.strip() for c in args.warm.split(",") if c.strip()}
        bad = {c for c in requested if not is_valid_context_name(c)}
        if bad:
            parser.error(f"invalid context name(s) in --warm: {', '.join(sorted(bad))}")
        WARM_CONTEXTS = requested
    if WARM_CONTEXTS:
        print(f"Keeping warm: {', '.join(sorted(WARM_CONTEXTS))}")
    parse_cluster.KEEP_RAW_DUMPS = parse_cluster.KEEP_RAW_DUMPS or args.keep_raw_dumps

    SYNC_INTERVAL_SECONDS = max(5, args.interval)

    # Initialize DVR database schemas on boot
    dvr_db.init_db()

    # Dumps written by earlier versions held literal env values (often credentials): scrub them once
    parse_cluster.ensure_data_dir()
    scrubbed = redaction.scrub_data_dir(parse_cluster.DATA_DIR, parse_cluster.write_file_atomic)
    if scrubbed:
        print(f"✔ Redacted secret values from {scrubbed} existing cluster dump(s) in {parse_cluster.DATA_DIR}")
    report_stale_raw_dumps()
    if args.brand:
        brand.set_brand_dir(args.brand)
    for line in brand.describe():
        print(line)

    # Discover active context dynamically on startup
    init_active_context()

    with active_context_lock:
        ctx = active_context

    print(f"Bootstrapping cluster state for context: {ctx}...")
    if refresh_context(ctx) is None and ctx != "demo":
        # Stay on the real context and keep retrying in the background: silently switching to demo
        # (as earlier versions did) left Lex stuck on sample data after a transient failure at startup.
        error = get_sync_status(ctx).get("lastError") or "unknown error"
        print(f"▲ Initial scrape of '{ctx}' failed: {error}")
        print(f"▲ Staying on '{ctx}' and retrying every {SYNC_INTERVAL_SECONDS}s. "
              f"Check kubectl access (e.g. `kubectl --context={ctx} get nodes`), or pick 'demo' in the UI for sample data.")

    atexit.register(stop_all_watchers)   # never leave kubectl watch processes behind

    # Launch background thread
    t = threading.Thread(target=run_bg_sync, args=(SYNC_INTERVAL_SECONDS,), daemon=True)
    t.start()

    # Serve HTTP (threaded, so a slow kubectl call never blocks other requests)
    server_address = (BIND_ADDRESS, args.port)
    httpd = http.server.ThreadingHTTPServer(server_address, LocalAPIServer)
    httpd.daemon_threads = True
    print(f"============================================================")
    print(f"🚀 Lex Server running successfully!")
    print(f"🔗 Local Web Interface: http://{BIND_ADDRESS}:{args.port}/")
    print(f"============================================================")

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down server...")
        stop_all_watchers()
        httpd.server_close()

if __name__ == '__main__':
    main()
