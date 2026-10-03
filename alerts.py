#!/usr/bin/env python3
"""
Alert engine for Lex.

Evaluates a compiled cluster state (from parse_cluster) into a list of actionable alerts. This is the
single source of truth for "what is broken": the UI colors buildings and rooms from these alerts, and
DVR incident summaries are derived from them.

Alerts carry a stable `since` timestamp (never a duration), so an unchanged cluster produces an identical
alert list on every scrape and the state's ETag stays the same.
"""

import datetime

CRITICAL = "critical"
WARNING = "warning"

NODE_DOWN_GRACE_MINUTES = 2      # Ready must be false/unknown this long before it's an alert
NODE_JOIN_GRACE_MINUTES = 10     # newly created nodes are "coming online", not down
UNSCHEDULABLE_MINUTES = 5        # shorter waits are routine while autoscalers add capacity
WORKLOAD_UNAVAILABLE_MINUTES = 5 # rollouts and restarts dip availability briefly
STUCK_INIT_MINUTES = 30
OOM_WINDOW_MINUTES = 60
RESTART_WINDOW_MINUTES = 15
RESTART_THRESHOLD = 3
CAPACITY_HEADROOM_PERCENT = 85

UNINITIALIZED_TAINTS = ("node.cloudprovider.kubernetes.io/uninitialized", "karpenter.sh/unregistered")
IMAGE_PULL_STATUSES = ("ImagePullBackOff", "ErrImagePull", "InvalidImageName", "ErrImageNeverPull")
PRESSURE_CONDITIONS = (
    ("MemoryPressure", "Memory pressure"),
    ("DiskPressure", "Disk pressure"),
    ("PIDPressure", "PID pressure"),
    ("NetworkUnavailable", "Network unavailable"),
)


def _parse_ts(ts):
    if not ts:
        return None
    try:
        return datetime.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _minutes_between(earlier, later):
    return (later - earlier).total_seconds() / 60.0


class AlertTracker:
    """Per-context memory across scrapes: when each alert was first seen, and recent restart counts."""

    def __init__(self):
        self.first_seen = {}   # alert id -> datetime
        self.restarts = {}     # "namespace/name" -> [(datetime, restart_count), ...]

    def since(self, alert_id, now, known=None):
        """The alert's start time: a known timestamp from the API if available, else when we first saw it."""
        if known is not None:
            self.first_seen[alert_id] = known
            return known
        return self.first_seen.setdefault(alert_id, now)

    def restart_increase(self, key, count, now):
        """Restarts added within the restart window (also records this observation)."""
        history = [(t, c) for t, c in self.restarts.get(key, []) if _minutes_between(t, now) <= RESTART_WINDOW_MINUTES]
        history.append((now, count))
        self.restarts[key] = history
        return max(0, count - min(c for _, c in history))

    def prune(self, active_ids, seen_pod_keys):
        self.first_seen = {k: v for k, v in self.first_seen.items() if k in active_ids}
        self.restarts = {k: v for k, v in self.restarts.items() if k in seen_pod_keys}


_trackers = {}


def tracker_for(context):
    return _trackers.setdefault(context or "default", AlertTracker())


def _node_age_minutes(node, now):
    created = _parse_ts(node.get("creationTimestamp"))
    return _minutes_between(created, now) if created else None


def is_node_coming_online(node, now):
    """Not Ready because it's new or still being initialized by the cloud provider / Karpenter."""
    if any((t or {}).get("key") in UNINITIALIZED_TAINTS for t in node.get("taints") or []):
        return True
    age = _node_age_minutes(node, now)
    return age is not None and age < NODE_JOIN_GRACE_MINUTES


def is_node_decommissioning(node):
    cond = node.get("conditions") or {}
    return bool(node.get("unschedulable")) or cond.get("ReadyReason") in ("NodeDecommissioning", "NodeDraining")


def _truncate(text, limit=240):
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def evaluate(state, context=None, now=None, tracker=None):
    """Returns the alert list for a compiled state, ordered critical-first then oldest-first."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    tracker = tracker if tracker is not None else tracker_for(context)
    alerts = []
    considered = set()   # includes alerts still inside their grace period, so their first-seen time is kept

    def add(alert_id, severity, alert_type, title, subject, detail, known_since=None, min_minutes=0):
        considered.add(alert_id)
        since = tracker.since(alert_id, now, known_since)
        if min_minutes and _minutes_between(since, now) < min_minutes:
            return  # still within its grace period
        alerts.append({
            "id": alert_id,
            "severity": severity,
            "type": alert_type,
            "title": title,
            "subject": subject,
            "detail": _truncate(detail),
            "since": _iso(since),
        })

    nodes = state.get("nodes") or []
    seen_pod_keys = set()
    total_alloc_mem = total_alloc_cpu = total_req_mem = total_req_cpu = 0.0

    for node in nodes:
        name = node.get("name")
        cond = node.get("conditions") or {}
        node_subject = {"kind": "node", "name": name}

        # Node not ready (excluding nodes that are joining or being drained on purpose)
        ready = cond.get("Ready")
        if ready is not None and ready != "True" and not is_node_coming_online(node, now) and not is_node_decommissioning(node):
            reason = cond.get("ReadyReason") or ("NodeStatusUnknown" if ready == "Unknown" else "NotReady")
            message = cond.get("ReadyMessage") or ("Kubelet stopped posting node status." if ready == "Unknown" else "")
            add(f"node-not-ready/{name}", CRITICAL, "NodeNotReady", "Node not ready", node_subject,
                f"{reason}: {message}" if message else reason,
                known_since=_parse_ts(cond.get("ReadySince")), min_minutes=NODE_DOWN_GRACE_MINUTES)

        for cond_type, label in PRESSURE_CONDITIONS:
            if cond.get(cond_type) == "True":
                add(f"node-pressure/{cond_type}/{name}", CRITICAL, "NodePressure", label, node_subject,
                    cond.get(cond_type + "Message") or cond.get(cond_type + "Reason") or label,
                    known_since=_parse_ts(cond.get(cond_type + "Since")))

        if not is_node_decommissioning(node):
            total_alloc_mem += node.get("maxMemoryGB") or 0
            total_alloc_cpu += node.get("maxCPUCores") or 0

        for pod in node.get("pods") or []:
            ns, pod_name = pod.get("namespace") or "default", pod.get("name")
            key = f"{ns}/{pod_name}"
            seen_pod_keys.add(key)
            subject = {"kind": "pod", "namespace": ns, "name": pod_name, "node": name}
            status = str(pod.get("status") or "")
            restarts = pod.get("restarts") or 0
            if not is_node_decommissioning(node):
                total_req_mem += pod.get("memoryGB") or 0
                total_req_cpu += pod.get("cpuCores") or 0

            increase = tracker.restart_increase(key, restarts, now)
            last_term = pod.get("lastTermination") or {}
            finished = _parse_ts(last_term.get("finishedAt"))

            if "CrashLoopBackOff" in status:
                add(f"crashloop/{key}", CRITICAL, "CrashLoop", "Crash looping", subject,
                    f"{status} · {restarts} restarts" + (f" · last exit: {last_term.get('reason')}" if last_term.get("reason") else ""))
            elif increase >= RESTART_THRESHOLD:
                add(f"restarts/{key}", CRITICAL, "FrequentRestarts", "Restarting frequently", subject,
                    f"+{increase} restarts in the last {RESTART_WINDOW_MINUTES}m ({restarts} total)")

            if last_term.get("reason") == "OOMKilled" and finished and _minutes_between(finished, now) <= OOM_WINDOW_MINUTES:
                add(f"oom/{key}", CRITICAL, "OOMKilled", "OOMKilled", subject,
                    f"Container '{last_term.get('container')}' was killed for exceeding its memory limit", known_since=finished)

            if any(s in status for s in IMAGE_PULL_STATUSES):
                add(f"image-pull/{key}", WARNING, "ImagePull", "Image pull failing", subject, status)
            elif "CrashLoopBackOff" not in status and any(w in status.lower() for w in ("error", "fail", "invalid")):
                add(f"container-error/{key}", CRITICAL, "ContainerError", "Container failing to start", subject, status)
            elif status.startswith("Init:") or status in ("ContainerCreating", "PodInitializing"):
                created = _parse_ts(pod.get("creationTimestamp"))
                if created and _minutes_between(created, now) > STUCK_INIT_MINUTES:
                    add(f"stuck-init/{key}", WARNING, "StuckInitializing", "Stuck initializing", subject,
                        f"{status} since creation", known_since=created)

    # Pods the scheduler can't place
    for pod in state.get("unscheduledPods") or []:
        if pod.get("reason") != "Unschedulable":
            continue
        ns, pod_name = pod.get("namespace") or "default", pod.get("name")
        add(f"unschedulable/{ns}/{pod_name}", WARNING, "Unschedulable", "Can't be scheduled",
            {"kind": "pod", "namespace": ns, "name": pod_name, "lobby": True},
            pod.get("message") or "Unschedulable",
            known_since=_parse_ts(pod.get("since") or pod.get("creationTimestamp")), min_minutes=UNSCHEDULABLE_MINUTES)

    # Recently OOMKilled pods that have already terminated (e.g. Jobs)
    for pod in state.get("failedPods") or []:
        last_term = pod.get("lastTermination") or {}
        finished = _parse_ts(last_term.get("finishedAt"))
        if last_term.get("reason") == "OOMKilled" and finished and _minutes_between(finished, now) <= OOM_WINDOW_MINUTES:
            ns, pod_name = pod.get("namespace") or "default", pod.get("name")
            add(f"oom/{ns}/{pod_name}", CRITICAL, "OOMKilled", "OOMKilled",
                {"kind": "pod", "namespace": ns, "name": pod_name, "node": pod.get("nodeName"), "failed": True},
                f"Container '{last_term.get('container')}' was killed for exceeding its memory limit", known_since=finished)

    # Workloads with fewer ready replicas than desired: none ready is critical, partially ready a warning
    for w in state.get("workloads") or []:
        desired, ready = w.get("desired") or 0, w.get("ready") or 0
        if desired <= 0 or ready >= desired:
            continue
        kind, ns, name = w.get("kind"), w.get("namespace") or "default", w.get("name")
        short = {"Deployment": "deploy", "StatefulSet": "sts", "DaemonSet": "ds"}.get(kind, kind.lower())
        detail = (f"ProgressDeadlineExceeded: {w.get('message')}" if w.get("stalled") and w.get("message")
                  else f"{desired - ready} of {desired} replica{'s' if desired != 1 else ''} not ready"
                       + (f" · {w.get('message')}" if w.get("message") else ""))
        add(f"workload/{short}/{ns}/{name}", CRITICAL if ready == 0 else WARNING, "WorkloadUnavailable",
            f"{kind} {ready}/{desired} ready", {"kind": "workload", "workloadKind": kind, "namespace": ns, "name": name},
            detail, known_since=_parse_ts(w.get("unavailableSince")), min_minutes=WORKLOAD_UNAVAILABLE_MINUTES)

    # Cluster capacity headroom (requests vs allocatable)
    for resource, requested, allocatable, unit in (("memory", total_req_mem, total_alloc_mem, "GB"),
                                                   ("cpu", total_req_cpu, total_alloc_cpu, "cores")):
        if allocatable > 0:
            pct = requested / allocatable * 100
            if pct >= CAPACITY_HEADROOM_PERCENT:
                add(f"capacity/{resource}", WARNING, "CapacityHeadroom",
                    f"{'Memory' if resource == 'memory' else 'CPU'} requests at {int(pct)}%", {"kind": "cluster"},
                    f"{requested:.1f} of {allocatable:.1f} {unit} allocatable is requested; new pods may not fit")

    tracker.prune(considered, seen_pod_keys)
    alerts.sort(key=lambda a: (0 if a["severity"] == CRITICAL else 1, a["since"], a["id"]))
    return alerts
