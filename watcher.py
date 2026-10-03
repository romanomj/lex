#!/usr/bin/env python3
"""
Live ingestion for one kubectl context: list once, then stream changes (Kubernetes watches).

Instead of re-downloading every pod each sync interval, a ContextWatcher:
  1. lists nodes, pods and workloads via `kubectl get --raw` and keeps the lists' resourceVersion,
  2. watches /api/v1/nodes and /api/v1/pods from that resourceVersion, applying ADDED / MODIFIED /
     DELETED events to an in-memory copy (sanitized on arrival, like the dumps on disk),
  3. recompiles the visualizer state when something changed (debounced, at most every COMPILE_MIN_INTERVAL),
     and at least every `poll_interval` so time-based alerts (grace periods, OOM windows) still mature,
  4. re-lists workloads + PDBs every WORKLOADS_SECONDS, and everything every RELIST_SECONDS to correct drift.

Watch streams that expire (410 Gone) trigger a re-list; dropped streams reconnect with backoff; a quiet
stream that stops responding is restarted by a watchdog. If the API refuses watches (RBAC), the watcher
falls back to periodic re-listing.
"""

import json
import time
import threading
import subprocess

import parse_cluster
import redaction

LIST_PATHS = {"nodes": "/api/v1/nodes", "pods": "/api/v1/pods"}
WORKLOAD_PATHS = [("Deployment", "/apis/apps/v1/deployments"), ("StatefulSet", "/apis/apps/v1/statefulsets"),
                  ("DaemonSet", "/apis/apps/v1/daemonsets"), ("PodDisruptionBudget", "/apis/policy/v1/poddisruptionbudgets")]
WATCH_TIMEOUT_SECONDS = 540       # the API server ends each watch after this; we resume from the last resourceVersion
WATCHDOG_SECONDS = 660            # no events/bookmarks/reconnects for this long: assume a dead connection, reconnect
RELIST_SECONDS = 600
WORKLOADS_SECONDS = 60
COMPILE_MIN_INTERVAL = 2.0
DEBOUNCE_SECONDS = 0.5
LIST_TIMEOUT_SECONDS = 90


def _object_key(obj):
    md = obj.get("metadata") or {}
    return f"{md.get('namespace', '')}/{md.get('name')}"


class ContextWatcher:
    def __init__(self, context, on_compiled, on_error, poll_interval=30, log=print):
        self.context = context
        self.on_compiled = on_compiled        # callback(context, state)
        self.on_error = on_error              # callback(context, message)
        self.poll_interval = poll_interval
        self.log = log
        self.lock = threading.Lock()
        self.objects = {"nodes": {}, "pods": {}}
        self.rv = {"nodes": None, "pods": None}
        self.workloads_data = None
        self.workloads_sig = None
        self.stop_event = threading.Event()
        self.dirty = threading.Event()
        self.expired = threading.Event()
        self.first_compile = threading.Event()
        self.procs = set()
        self.mode = "starting"                # starting | watch | poll | error
        self.watch_forbidden = False
        self.last_activity = time.time()      # last event, bookmark or (re)connect
        self.healthy_at = None                # last time the data was known to be current
        self.last_error = None
        self.counters = {"events": 0, "lists": 0, "watches": 0, "compiles": 0, "expired": 0}
        self.thread = threading.Thread(target=self._run, daemon=True, name=f"lex-watch:{context}")

    # ---------- lifecycle ----------
    def start(self):
        self.thread.start()
        return self

    def stop(self):
        self.stop_event.set()
        self.expired.set()
        self._kill_procs()

    def wait_ready(self, timeout):
        """Blocks until the first state has been compiled (or timeout). Returns True when ready."""
        deadline = time.time() + timeout
        while time.time() < deadline and not self.stop_event.is_set():
            if self.first_compile.wait(0.25):
                return True
        return self.first_compile.is_set()

    def status(self):
        return {"mode": self.mode, "healthyAt": self.healthy_at, "lastError": self.last_error, **self.counters}

    def _kill_procs(self):
        with self.lock:
            procs = list(self.procs)
        for p in procs:
            try:
                p.kill()
            except Exception:
                pass

    # ---------- kubectl ----------
    def _raw(self, path):
        cmd = ["kubectl", f"--context={self.context}", "--request-timeout=60s", "get", "--raw", path]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=LIST_TIMEOUT_SECONDS)
        if res.returncode != 0:
            raise RuntimeError((res.stderr or "kubectl failed").strip()[:300])
        return json.loads(res.stdout)

    def _relist(self):
        """Full list of nodes + pods (+ workloads). Resets the in-memory copy and the watch resourceVersions."""
        lists = {}
        for resource, path in LIST_PATHS.items():
            data = self._raw(path)
            redaction.sanitize_list_for_disk(data)
            lists[resource] = data
        with self.lock:
            for resource, data in lists.items():
                self.objects[resource] = {_object_key(o): o for o in data.get("items") or []}
                self.rv[resource] = (data.get("metadata") or {}).get("resourceVersion")
        self.counters["lists"] += 1
        self._list_workloads(force=True)
        # Keep a sanitized on-disk snapshot of the raw lists (offline tools, demo-style inspection)
        nodes_file, pods_file, _ = parse_cluster.get_file_paths(self.context)
        parse_cluster.write_file_atomic(nodes_file, json.dumps(lists["nodes"], separators=(",", ":")))
        parse_cluster.write_file_atomic(pods_file, json.dumps(lists["pods"], separators=(",", ":")))

    def _list_workloads(self, force=False):
        items = []
        for kind, path in WORKLOAD_PATHS:
            try:
                data = self._raw(path)
            except Exception:
                continue   # e.g. RBAC: carry on without this kind
            for obj in data.get("items") or []:
                obj["kind"] = kind      # raw list items omit their kind
                items.append(obj)
        wl = redaction.sanitize_list_for_disk({"items": items})
        sig = parse_cluster.fingerprint([[o["kind"], o.get("metadata", {}).get("namespace"), o.get("metadata", {}).get("name"),
                                          o.get("spec", {}).get("replicas"), o.get("status")] for o in items])
        if force or sig != self.workloads_sig:
            self.workloads_sig = sig
            self.workloads_data = wl
            parse_cluster.write_file_atomic(parse_cluster.get_workloads_path(self.context), json.dumps(wl, separators=(",", ":")))
            if not force:
                self.dirty.set()

    # ---------- compile ----------
    def _compile(self):
        with self.lock:
            nodes = {"items": list(self.objects["nodes"].values())}
            pods = {"items": list(self.objects["pods"].values())}
            wl = self.workloads_data
        _, _, out_file = parse_cluster.get_file_paths(self.context)
        state = parse_cluster.compile_state(nodes, pods, wl, self.context, self.context, output_file=out_file, verbose=False)
        self.counters["compiles"] += 1
        self.healthy_at = time.time()
        self.first_compile.set()
        self.on_compiled(self.context, state)

    # ---------- watch streams ----------
    def _watch(self, resource):
        backoff = 1.0
        while not self.stop_event.is_set() and not self.expired.is_set():
            with self.lock:
                rv = self.rv[resource]
            path = (f"{LIST_PATHS[resource]}?watch=1&allowWatchBookmarks=true"
                    f"&timeoutSeconds={WATCH_TIMEOUT_SECONDS}&resourceVersion={rv}")
            cmd = ["kubectl", f"--context={self.context}", "get", "--raw", path]
            try:
                proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
            except OSError as e:
                self.last_error = f"Could not start kubectl watch: {e}"
                return
            with self.lock:
                self.procs.add(proc)
            self.counters["watches"] += 1
            self.last_activity = time.time()
            try:
                for line in proc.stdout:
                    if self.stop_event.is_set() or self.expired.is_set():
                        break
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    self.last_activity = time.time()
                    etype, obj = event.get("type"), event.get("object") or {}
                    if etype == "ERROR":
                        if obj.get("code") == 410:   # resourceVersion too old: re-list
                            self.counters["expired"] += 1
                            self.expired.set()
                        else:
                            self.last_error = f"Watch error: {obj.get('message') or obj}"
                        break
                    new_rv = (obj.get("metadata") or {}).get("resourceVersion")
                    if etype == "BOOKMARK":
                        with self.lock:
                            self.rv[resource] = new_rv or self.rv[resource]
                        continue
                    redaction.sanitize_list_for_disk({"items": [obj]})
                    with self.lock:
                        if etype == "DELETED":
                            self.objects[resource].pop(_object_key(obj), None)
                        else:
                            self.objects[resource][_object_key(obj)] = obj
                        if new_rv:
                            self.rv[resource] = new_rv
                    self.counters["events"] += 1
                    self.dirty.set()
            finally:
                try:
                    proc.kill()
                except Exception:
                    pass
                proc.wait()
                stderr = proc.stderr.read() if proc.stderr else ""
                with self.lock:
                    self.procs.discard(proc)
            if self.stop_event.is_set() or self.expired.is_set():
                return
            if proc.returncode not in (0, -9, None):
                lowered = stderr.lower()
                if "forbidden" in lowered or "(403)" in lowered:
                    self.watch_forbidden = True
                    self.expired.set()
                    return
                if "too old" in lowered or "(410)" in lowered or "gone" in lowered:
                    self.counters["expired"] += 1
                    self.expired.set()
                    return
                self.last_error = f"Watch stream ended: {stderr.strip()[:200]}"
                time.sleep(backoff)
                backoff = min(backoff * 2, 30)
            else:
                backoff = 1.0   # normal end (server-side timeout): resume immediately from the last resourceVersion

    # ---------- main loop ----------
    def _run(self):
        backoff = 2.0
        while not self.stop_event.is_set():
            try:
                self._relist()
                self._compile()
                self.last_error = None
                backoff = 2.0
                if self.watch_forbidden:
                    self.mode = "poll"
                    self._poll_until_stopped()
                    return
                self.expired.clear()
                threads = [threading.Thread(target=self._watch, args=(r,), daemon=True, name=f"lex-watch:{self.context}:{r}")
                           for r in LIST_PATHS]
                for t in threads:
                    t.start()
                self.mode = "watch"
                last_compile = time.time()
                next_workloads = time.time() + WORKLOADS_SECONDS
                next_relist = time.time() + RELIST_SECONDS
                while not self.stop_event.is_set() and not self.expired.is_set():
                    if self.dirty.wait(timeout=1.0):
                        time.sleep(DEBOUNCE_SECONDS)                      # let a burst of events settle
                        wait = COMPILE_MIN_INTERVAL - (time.time() - last_compile)
                        if wait > 0:
                            self.stop_event.wait(wait)
                        self.dirty.clear()
                        self._compile()
                        last_compile = time.time()
                    elif time.time() - last_compile >= self.poll_interval:
                        self._compile()                                   # quiet cluster: let grace periods elapse
                        last_compile = time.time()
                    now = time.time()
                    if all(t.is_alive() for t in threads) and now - self.last_activity < WATCHDOG_SECONDS:
                        self.healthy_at = now                             # streams connected: data is current
                    elif now - self.last_activity >= WATCHDOG_SECONDS:
                        self._kill_procs()                                # silent dead connection: reconnect
                        self.last_activity = now
                    if now >= next_workloads:
                        self._list_workloads()
                        next_workloads = now + WORKLOADS_SECONDS
                    if now >= next_relist:
                        break                                             # periodic full re-list
                    if not any(t.is_alive() for t in threads):
                        break
                self.expired.set()
                self._kill_procs()
                for t in threads:
                    t.join(timeout=5)
                if self.watch_forbidden and not self.stop_event.is_set():
                    self.log(f"▲ Watching is not permitted for '{self.context}'; falling back to polling every {self.poll_interval}s")
            except Exception as e:
                self.mode = "error"
                self.last_error = str(e)
                self.on_error(self.context, str(e))
                self.stop_event.wait(backoff)
                backoff = min(backoff * 2, 60)

    def _poll_until_stopped(self):
        while not self.stop_event.wait(self.poll_interval):
            try:
                self._relist()
                self._compile()
                self.last_error = None
            except Exception as e:
                self.last_error = str(e)
                self.on_error(self.context, str(e))
