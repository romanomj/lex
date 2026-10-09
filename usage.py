#!/usr/bin/env python3
"""
Actual resource usage for the rightsizing lens (F-33), from the Kubernetes metrics API (metrics-server).

Usage is kept out of the compiled cluster state on purpose: it changes on every sample, and putting it in the
state would change the state's ETag and rebuild every building each time. Instead the server samples usage
on its own cadence and the UI fetches it from /api/v1/usage only while the lens is open.

Each context keeps per-pod peaks in 5-minute buckets for the last PEAK_WINDOW_MINUTES, so recommendations
can be based on the recent peak rather than a single moment. The window only covers the time Lex has been
running (reported as `windowMinutes`).
"""

import json
import time
import hashlib
import threading
import subprocess

import parse_cluster

BUCKET_SECONDS = 300
PEAK_WINDOW_MINUTES = 60
UNAVAILABLE_RETRY_SECONDS = 600
HEADROOM = 1.2                 # recommended request = peak × HEADROOM
MIN_CPU_CORES = 0.01
MIN_MEMORY_GB = 32 / 1024


def _kubectl_raw(context, path):
    res = subprocess.run(["kubectl", f"--context={context}", "--request-timeout=20s", "get", "--raw", path],
                         capture_output=True, text=True, timeout=30)
    if res.returncode != 0:
        err = (res.stderr or "kubectl failed").strip()
        raise RuntimeError(err[:300])
    return json.loads(res.stdout)


def _pod_usage(item):
    cpu = mem = 0.0
    for c in item.get("containers") or []:
        u = c.get("usage") or {}
        cpu += parse_cluster.parse_cpu_to_cores(u.get("cpu"))
        mem += parse_cluster.parse_memory_to_gb(u.get("memory"))
    return cpu, mem


class UsageTracker:
    def __init__(self, context):
        self.context = context
        self.lock = threading.Lock()
        self.buckets = {}          # bucket start -> {"pods": {key: [cpuMax, memMax]}, "nodes": {...}}
        self.current = {"pods": {}, "nodes": {}}
        self.sampled_at = None
        self.first_sample_at = None
        self.available = None      # None = not tried yet
        self.reason = None
        self.last_attempt = 0.0

    def sample(self):
        """One metrics API read. Returns True on success."""
        self.last_attempt = time.time()
        try:
            pods = _kubectl_raw(self.context, "/apis/metrics.k8s.io/v1beta1/pods")
            nodes = _kubectl_raw(self.context, "/apis/metrics.k8s.io/v1beta1/nodes")
        except Exception as e:
            msg = str(e)
            self.available = False
            self.reason = ("metrics-server is not installed in this cluster (the metrics.k8s.io API is missing)"
                           if "could not find the requested resource" in msg or "NotFound" in msg
                           else f"Could not read the metrics API: {msg}")
            return False
        now = time.time()
        pod_usage = {}
        for item in pods.get("items") or []:
            md = item.get("metadata") or {}
            pod_usage[f"{md.get('namespace')}/{md.get('name')}"] = _pod_usage(item)
        node_usage = {}
        for item in nodes.get("items") or []:
            u = item.get("usage") or {}
            node_usage[(item.get("metadata") or {}).get("name")] = (parse_cluster.parse_cpu_to_cores(u.get("cpu")),
                                                                    parse_cluster.parse_memory_to_gb(u.get("memory")))
        self._record(now, pod_usage, node_usage)
        return True

    def _record(self, now, pod_usage, node_usage):
        start = int(now // BUCKET_SECONDS) * BUCKET_SECONDS
        with self.lock:
            bucket = self.buckets.setdefault(start, {"pods": {}, "nodes": {}})
            for kind, values in (("pods", pod_usage), ("nodes", node_usage)):
                b = bucket[kind]
                for key, (cpu, mem) in values.items():
                    peak = b.get(key)
                    b[key] = [max(cpu, peak[0]), max(mem, peak[1])] if peak else [cpu, mem]
            cutoff = start - PEAK_WINDOW_MINUTES * 60
            for old in [k for k in self.buckets if k <= cutoff]:
                del self.buckets[old]
            self.current = {"pods": pod_usage, "nodes": node_usage}
            self.sampled_at = now
            if self.first_sample_at is None:
                self.first_sample_at = now
            self.available = True
            self.reason = None

    def snapshot(self):
        with self.lock:
            peaks = {"pods": {}, "nodes": {}}
            for bucket in self.buckets.values():
                for kind in ("pods", "nodes"):
                    for key, (cpu, mem) in bucket[kind].items():
                        p = peaks[kind].get(key)
                        peaks[kind][key] = [max(cpu, p[0]), max(mem, p[1])] if p else [cpu, mem]

            def merge(kind):
                out = {}
                for key, (cpu, mem) in self.current[kind].items():
                    pk = peaks[kind].get(key, [cpu, mem])
                    out[key] = [round(cpu, 4), round(mem, 4), round(pk[0], 4), round(pk[1], 4)]
                return out

            window = 0
            if self.first_sample_at and self.sampled_at:
                window = min(PEAK_WINDOW_MINUTES, round((self.sampled_at - self.first_sample_at) / 60))
            return {
                "available": bool(self.available),
                "reason": self.reason,
                "sampledAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.sampled_at)) if self.sampled_at else None,
                "windowMinutes": window,
                "peakWindowMinutes": PEAK_WINDOW_MINUTES,
                "headroom": HEADROOM,
                # [cpuCores, memoryGB, peakCpuCores, peakMemoryGB]
                "fields": ["cpu", "memoryGB", "peakCpu", "peakMemoryGB"],
                "pods": merge("pods"),
                "nodes": merge("nodes"),
            }

    def due(self, interval):
        if self.available is False:
            return time.time() - self.last_attempt >= UNAVAILABLE_RETRY_SECONDS
        return time.time() - self.last_attempt >= interval * 0.9


_trackers = {}
_trackers_lock = threading.Lock()


def tracker_for(context):
    with _trackers_lock:
        if context not in _trackers:
            _trackers[context] = UsageTracker(context)
        return _trackers[context]


def sample_if_due(context, interval):
    t = tracker_for(context)
    if t.due(interval):
        t.sample()
    return t


def _unit_hash(text):
    return int(hashlib.sha1(text.encode("utf-8")).hexdigest()[:8], 16) / 0xFFFFFFFF


def synthetic_usage(state):
    """Plausible, stable usage for the demo cluster: most pods use 15-70% of their requests, a few run hot."""
    pods, nodes = {}, {}
    for node in state.get("nodes") or []:
        ncpu = nmem = 0.0
        for p in node.get("pods") or []:
            key = f"{p.get('namespace')}/{p.get('name')}"
            h = _unit_hash(key)
            ratio = 1.15 if h > 0.9 else 0.15 + h * 0.6
            cpu = (p.get("cpuCores") or 0.1) * ratio * (0.8 + _unit_hash(key + "c") * 0.4)
            mem = (p.get("memoryGB") or 0.1) * ratio
            peak = 1.1 + _unit_hash(key + "p") * 0.3
            pods[key] = [round(cpu, 4), round(mem, 4), round(cpu * peak, 4), round(min(mem * peak, mem * 1.4), 4)]
            ncpu += cpu
            nmem += mem
        nodes[node.get("name")] = [round(ncpu * 1.1, 4), round(nmem * 1.1 + 0.6, 4), round(ncpu * 1.3, 4), round(nmem * 1.2 + 0.6, 4)]
    return {
        "available": True, "synthetic": True, "reason": None,
        "sampledAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "windowMinutes": PEAK_WINDOW_MINUTES,
        "peakWindowMinutes": PEAK_WINDOW_MINUTES, "headroom": HEADROOM,
        "fields": ["cpu", "memoryGB", "peakCpu", "peakMemoryGB"], "pods": pods, "nodes": nodes,
    }
