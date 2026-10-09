#!/usr/bin/env python3
"""
Vital-signs history for Lex (always on, independent of DVR recording).

Each successful scrape stores one small summary row per context; the UI draws its sparklines and the
Jumbotron from these. Rows live in the DVR SQLite database and are kept for METRICS_RETENTION_DAYS.
"""

import json
import math
import datetime
import threading

import dvr_db

METRICS_RETENTION_DAYS = 7
MAX_POINTS = 120   # points returned per window after downsampling

_previous_restarts = {}   # context -> {"ns/name": restarts} from the previous scrape
_lock = threading.Lock()


def compute_point(context, state, now=None):
    """One summary point for a compiled state. Restarts are counted per pod since the previous scrape."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    nodes = state.get("nodes") or []
    alloc_mem = alloc_cpu = req_mem = req_cpu = 0.0
    running = pending = 0
    restarts_now = {}
    nodes_ready = 0
    run_rate = 0.0
    for node in nodes:
        cond = node.get("conditions") or {}
        if cond.get("Ready") == "True":
            nodes_ready += 1
        if not node.get("unschedulable"):
            alloc_mem += node.get("maxMemoryGB") or 0
            alloc_cpu += node.get("maxCPUCores") or 0
        cost = node.get("costDetails") or {}
        run_rate += cost.get("hourlyCost") or 0
        for pod in node.get("pods") or []:
            req_mem += pod.get("memoryGB") or 0
            req_cpu += pod.get("cpuCores") or 0
            status = str(pod.get("status") or "")
            if status == "Running":
                running += 1
            elif status.startswith("Init:") or status in ("Pending", "ContainerCreating", "PodInitializing"):
                pending += 1
            restarts_now[f"{pod.get('namespace')}/{pod.get('name')}"] = pod.get("restarts") or 0
    pending += len(state.get("unscheduledPods") or [])

    alerts = state.get("alerts") or []
    failing_pods = {f"{a['subject'].get('namespace')}/{a['subject'].get('name')}" for a in alerts
                    if (a.get("subject") or {}).get("kind") == "pod" and a.get("type") != "Unschedulable"}

    with _lock:
        previous = _previous_restarts.get(context, {})
        restarts_delta = sum(max(0, r - previous.get(k, r)) for k, r in restarts_now.items())
        _previous_restarts[context] = restarts_now

    return {
        "t": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "cpuReqPct": round(req_cpu / alloc_cpu * 100, 1) if alloc_cpu else 0,
        "memReqPct": round(req_mem / alloc_mem * 100, 1) if alloc_mem else 0,
        "running": running,
        "pending": pending,
        "failing": len(failing_pods),
        "restarts": restarts_delta,
        "nodesReady": nodes_ready,
        "nodesTotal": len(nodes),
        "runRate": round(run_rate, 4),
        "critical": sum(1 for a in alerts if a.get("severity") == "critical"),
        "warning": sum(1 for a in alerts if a.get("severity") == "warning"),
    }


def record(context, state):
    point = compute_point(context, state)
    conn = dvr_db.get_db_connection()
    try:
        conn.execute("INSERT INTO metrics_points (context, ts, data) VALUES (?, ?, ?)", (context, point["t"], json.dumps(point)))
        conn.commit()
    finally:
        conn.close()
    return point


def prune(max_age_days=METRICS_RETENTION_DAYS):
    cutoff = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=max_age_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn = dvr_db.get_db_connection()
    try:
        removed = conn.execute("DELETE FROM metrics_points WHERE ts < ?", (cutoff,)).rowcount
        conn.commit()
        return removed
    finally:
        conn.close()


def _downsample(points, start, end, buckets=MAX_POINTS):
    """Aggregates points into fixed time buckets: averages for levels, max for counts, sums for restarts."""
    if len(points) <= buckets:
        return points
    span = (end - start).total_seconds() / buckets
    grouped = {}
    for p in points:
        t = datetime.datetime.strptime(p["t"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)
        grouped.setdefault(min(buckets - 1, int((t - start).total_seconds() / span)), []).append(p)
    out = []
    for i in sorted(grouped):
        g = grouped[i]
        avg = lambda k: round(sum(p[k] for p in g) / len(g), 2)
        out.append({
            "t": g[-1]["t"],
            "cpuReqPct": avg("cpuReqPct"), "memReqPct": avg("memReqPct"), "runRate": avg("runRate"),
            "running": g[-1]["running"], "nodesReady": g[-1]["nodesReady"], "nodesTotal": g[-1]["nodesTotal"],
            "pending": max(p["pending"] for p in g), "failing": max(p["failing"] for p in g),
            "critical": max(p["critical"] for p in g), "warning": max(p["warning"] for p in g),
            "restarts": sum(p["restarts"] for p in g),
        })
    return out


def history(context, window_seconds, end=None):
    end = end or datetime.datetime.now(datetime.timezone.utc)
    start = end - datetime.timedelta(seconds=window_seconds)
    conn = dvr_db.get_db_connection()
    try:
        rows = conn.execute("SELECT data FROM metrics_points WHERE context = ? AND ts >= ? AND ts <= ? ORDER BY ts ASC",
                            (context, start.strftime("%Y-%m-%dT%H:%M:%SZ"), end.strftime("%Y-%m-%dT%H:%M:%SZ"))).fetchall()
    finally:
        conn.close()
    return _downsample([json.loads(r["data"]) for r in rows], start, end)


def synthetic_history(state, window_seconds, end=None):
    """Plausible sample history for the demo context (clearly flagged as synthetic by the API)."""
    end = end or datetime.datetime.now(datetime.timezone.utc)
    base = compute_point("demo-synthetic", state, end)
    n = MAX_POINTS
    out = []
    for i in range(n):
        f = i / (n - 1)
        t = end - datetime.timedelta(seconds=window_seconds * (1 - f))
        wave = math.sin(f * math.pi * 3) * 0.5 + math.sin(f * math.pi * 11) * 0.15
        incident = 1.0 if 0.62 < f < 0.74 else 0.0
        out.append({
            "t": t.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "cpuReqPct": round(max(0, base["cpuReqPct"] * (0.85 + 0.08 * wave)), 1) if f < 1 else base["cpuReqPct"],
            "memReqPct": round(max(0, base["memReqPct"] * (0.9 + 0.06 * wave + 0.05 * incident)), 1) if f < 1 else base["memReqPct"],
            "running": max(0, base["running"] - int(2 * incident)),
            "pending": base["pending"] if f > 0.8 else int(1 + incident * 3),
            "failing": base["failing"] if f > 0.8 else int(incident * 2),
            "restarts": int(incident * 4 + (1 if i % 17 == 0 else 0)),
            "nodesReady": base["nodesReady"] if f > 0.3 else base["nodesTotal"],
            "nodesTotal": base["nodesTotal"],
            "runRate": base["runRate"],
            "critical": base["critical"] if f > 0.8 else int(incident * 2),
            "warning": base["warning"] if f > 0.8 else 1,
        })
    return out
