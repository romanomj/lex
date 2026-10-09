#!/usr/bin/env python3
"""
"Why is my pod failing?" explainer (F-24).

Turns a full pod object plus its events (and, when known, its node's conditions and recent usage) into a
plain-English diagnosis: a headline, ranked findings with the evidence behind each one and what to try,
a per-container breakdown (state, last exit, probes, requests/limits) and the relevant events.

The engine is pure (no kubectl): server.py fetches the pod and its events and passes them in, so the rules
can be exercised with fixtures.
"""

import re
import datetime

import alerts
import parse_cluster

CRITICAL = "critical"
WARNING = "warning"
INFO = "info"
SEVERITY_RANK = {CRITICAL: 0, WARNING: 1, INFO: 2}

QUICK_CRASH_SECONDS = 30            # crashing this soon after start points at startup/config problems
RECENT_RESTART_MINUTES = 60         # restarts older than this are history, not an active problem
SLOW_CREATE_MINUTES = 5             # an init container running longer than this is worth explaining
STUCK_START_MINUTES = alerts.STUCK_INIT_MINUTES   # creating/initializing with no error event: same threshold as the alert
STUCK_TERMINATING_GRACE_SECONDS = 60
MEMORY_NEAR_LIMIT = 0.9             # peak usage at 90% of the limit is one spike away from an OOM kill
MAX_EVENTS = 12

SIGNALS = {1: "SIGHUP", 2: "SIGINT", 3: "SIGQUIT", 4: "SIGILL", 6: "SIGABRT", 7: "SIGBUS", 8: "SIGFPE",
           9: "SIGKILL", 11: "SIGSEGV", 13: "SIGPIPE", 15: "SIGTERM"}

EXIT_CODE_MEANINGS = {
    0: "exited successfully",
    1: "general application error",
    2: "invalid usage or arguments (often a config or flag parsing error)",
    126: "the command was found but isn't executable (permissions)",
    127: "the command wasn't found in the image",
    128: "the container runtime couldn't start the process",
    130: "interrupted (SIGINT)",
    134: "aborted (SIGABRT), usually a failed assertion or a native-code crash",
    137: "killed (SIGKILL)",
    139: "segmentation fault (SIGSEGV)",
    143: "terminated (SIGTERM)",
    255: "fatal error with an out-of-range exit status (often `exit(-1)`)",
}


# ---------- small helpers ----------

def _parse_ts(ts):
    if not ts:
        return None
    try:
        return datetime.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ") if dt else None


def human_duration(seconds):
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    if hours < 48:
        return f"{hours}h {minutes % 60}m" if minutes % 60 else f"{hours}h"
    return f"{hours // 24}d"


def _ago(dt, now):
    return f"{human_duration((now - dt).total_seconds())} ago" if dt else "at an unknown time"


def _truncate(text, limit=400):
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _plural(n, word, plural=None):
    return f"{n} {word if n == 1 else (plural or word + 's')}"


def format_bytes(value):
    """A memory quantity (bytes) in the unit people use for limits."""
    if value is None:
        return None
    gib = value / (1024 ** 3)
    if gib >= 1:
        return f"{gib:.1f} GiB".replace(".0 GiB", " GiB")
    return f"{round(value / (1024 ** 2))} MiB"


def format_cores(value):
    if value is None:
        return None
    return f"{round(value * 1000)}m" if value < 1 else f"{round(value, 2):g} cores" if value != 1 else "1 core"


def exit_code_meaning(code, signal=None):
    if signal:
        return f"killed by signal {SIGNALS.get(signal, signal)}"
    if code is None:
        return "unknown exit status"
    if code in EXIT_CODE_MEANINGS:
        return EXIT_CODE_MEANINGS[code]
    if 128 < code < 160:
        sig = code - 128
        return f"killed by signal {SIGNALS.get(sig, sig)}"
    return "application-specific error code"


def describe_probe(probe):
    """'HTTP GET :8080/healthz every 10s, timeout 1s, fails after 3 misses'."""
    if not probe:
        return None
    if probe.get("httpGet"):
        h = probe["httpGet"]
        what = f"HTTP{'S' if str(h.get('scheme', '')).upper() == 'HTTPS' else ''} GET :{h.get('port')}{h.get('path') or '/'}"
    elif probe.get("tcpSocket"):
        what = f"TCP :{probe['tcpSocket'].get('port')}"
    elif probe.get("grpc"):
        what = f"gRPC :{probe['grpc'].get('port')}"
    elif probe.get("exec"):
        cmd = " ".join(str(c) for c in probe["exec"].get("command") or [])
        what = f"exec `{_truncate(cmd, 60)}`"
    else:
        what = "probe"
    parts = [what, f"every {probe.get('periodSeconds', 10)}s", f"timeout {probe.get('timeoutSeconds', 1)}s",
             f"fails after {_plural(probe.get('failureThreshold', 3), 'miss', 'misses')}"]
    if probe.get("initialDelaySeconds"):
        parts.append(f"starts after {probe['initialDelaySeconds']}s")
    return ", ".join(parts)


def _resources(container):
    res = container.get("resources") or {}
    req, lim = res.get("requests") or {}, res.get("limits") or {}
    return {
        "requests": {k: req.get(k) for k in ("cpu", "memory") if req.get(k)},
        "limits": {k: lim.get(k) for k in ("cpu", "memory") if lim.get(k)},
        "memoryLimitBytes": parse_cluster.parse_quantity(lim.get("memory")) if lim.get("memory") else None,
        "memoryRequestBytes": parse_cluster.parse_quantity(req.get("memory")) if req.get("memory") else None,
    }


# ---------- events ----------

def _event_time(ev):
    series = ev.get("series") or {}
    return _parse_ts(ev.get("lastTimestamp") or series.get("lastObservedTime") or ev.get("eventTime") or ev.get("firstTimestamp"))


def _event_container(ev):
    m = re.search(r"\{([^}]+)\}", (ev.get("involvedObject") or {}).get("fieldPath") or "")
    return m.group(1) if m else None


def normalize_events(events_list, pod_uid=None):
    """kubectl event objects → compact dicts, newest first, limited to this pod (not an older namesake)."""
    out = []
    for ev in (events_list or {}).get("items", []) if isinstance(events_list, dict) else events_list or []:
        obj = ev.get("involvedObject") or {}
        if obj.get("kind") and obj.get("kind") != "Pod":
            continue
        if pod_uid and obj.get("uid") and obj.get("uid") != pod_uid:
            continue
        when = _event_time(ev)
        out.append({
            "type": ev.get("type") or "Normal",
            "reason": ev.get("reason") or "",
            "message": _truncate(ev.get("message") or ev.get("note") or "", 500),
            "count": (ev.get("series") or {}).get("count") or ev.get("count") or 1,
            "lastSeen": _iso(when),
            "_when": when,
            "container": _event_container(ev),
            "source": (ev.get("source") or {}).get("component") or ev.get("reportingComponent") or "",
        })
    out.sort(key=lambda e: e["_when"] or datetime.datetime.min.replace(tzinfo=datetime.timezone.utc), reverse=True)
    return out


def _find_events(events, reason=None, contains=None, container=None):
    found = []
    for ev in events:
        if reason and ev["reason"] != reason:
            continue
        if contains and contains.lower() not in ev["message"].lower():
            continue
        if container and ev["container"] and ev["container"] != container:
            continue
        found.append(ev)
    return found


def _probe_failures(events, kind, container=None):
    """Unhealthy events for a probe kind ('Liveness', 'Readiness', 'Startup')."""
    return _find_events(events, reason="Unhealthy", contains=f"{kind} probe", container=container)


def _event_evidence(ev, now):
    count = f" (×{ev['count']})" if ev["count"] and ev["count"] > 1 else ""
    return f"Event {ev['reason']}{count}, {_ago(ev['_when'], now) if ev['_when'] else 'time unknown'}: {ev['message']}"


# ---------- diagnosis ----------

class Diagnosis:
    def __init__(self, now):
        self.now = now
        self.findings = []
        self.actions = []
        self.transient = None   # an expected, self-resolving state (e.g. waiting for the autoscaler)

    def add(self, severity, title, explanation, evidence=None, fixes=None, container=None, key=None, context=False):
        """context=True: background (e.g. node pressure) ranked after the pod's own findings of the same severity."""
        if key and any(f.get("_key") == key for f in self.findings):
            return
        self.findings.append({
            "_context": context,
            "severity": severity,
            "title": title,
            "explanation": explanation,
            "evidence": [e for e in (evidence or []) if e],
            "fixes": [f for f in (fixes or []) if f],
            "container": container,
            "_key": key,
        })

    def action(self, action, label, container=None, previous=False):
        entry = {"action": action, "label": label}
        if container:
            entry["container"] = container
        if previous:
            entry["previous"] = True
        if entry not in self.actions:
            self.actions.append(entry)


def _image_pull(d, name, image, waiting, events):
    message = waiting.get("message") or ""
    pull_events = _find_events(events, reason="Failed", container=name) or _find_events(events, reason="Failed")
    text = " ".join([message] + [e["message"] for e in pull_events[:3]]).lower()
    evidence = [f"Image: {image}"] + ([f"Kubelet: {_truncate(message, 300)}"] if message else []) + \
               [_event_evidence(e, d.now) for e in pull_events[:2]]
    if waiting.get("reason") == "InvalidImageName":
        d.add(CRITICAL, f"Image name for '{name}' is invalid",
              f"The image reference '{image}' isn't a valid image name, so Kubernetes can't even try to pull it.",
              evidence, ["Fix the image field in the workload spec (check for typos, uppercase letters or a stray space)."], name)
    elif any(s in text for s in ("not found", "manifest unknown", "does not exist", "no such manifest")):
        d.add(CRITICAL, f"Image for '{name}' doesn't exist",
              f"The registry says the image '{image}' (or that tag) doesn't exist. Usually the tag was never pushed, "
              "was mistyped, or the CI build that should have pushed it failed.",
              evidence, ["Check that the tag exists in the registry.",
                         "Check the CI pipeline that builds and pushes this image.",
                         "Redeploy with a tag that exists."], name)
    elif any(s in text for s in ("unauthorized", "401", "403", "denied", "authentication required", "no basic auth", "forbidden")):
        d.add(CRITICAL, f"Not allowed to pull the image for '{name}'",
              f"The registry rejected the pull of '{image}': the node has no valid credentials for it.",
              evidence, ["Add or fix `imagePullSecrets` on the pod or its service account.",
                         "For ECR, check that the node role can read the repository (and for cross-account repos, the repository policy).",
                         "Check that the pull secret hasn't expired."], name)
    elif any(s in text for s in ("no such host", "i/o timeout", "timeout", "connection refused", "tls", "dial tcp", "network is unreachable")):
        d.add(CRITICAL, f"Can't reach the registry for '{name}'",
              f"The node couldn't connect to the registry hosting '{image}'. This is a network or DNS problem, not a problem with the image.",
              evidence, ["Check the node's egress to the registry (NAT gateway, VPC endpoints, firewall, proxy).",
                         "Check that the registry hostname resolves from the node."], name)
    elif "toomanyrequests" in text or "rate limit" in text:
        d.add(CRITICAL, f"Registry rate limit hit pulling '{name}'",
              f"The registry is throttling pulls of '{image}' (Docker Hub's anonymous limit is the usual culprit).",
              evidence, ["Authenticate pulls with an `imagePullSecret`, or mirror the image into your own registry."], name)
    else:
        d.add(CRITICAL, f"Can't pull the image for '{name}'",
              f"Kubernetes keeps failing to pull '{image}' and is backing off between attempts.",
              evidence, ["Read the full pull error in the events below.", "Try pulling the image by hand with the same credentials."], name)
    d.action("events", "See the pull errors in the pod's events")


def _config_error(d, name, waiting):
    message = waiting.get("message") or ""
    evidence = [f"Kubelet: {_truncate(message, 300)}"] if message else []
    m = re.search(r'(secret|configmap)s? "([^"]+)" not found', message, re.I)
    k = re.search(r'couldn\'t find key (\S+) in (Secret|ConfigMap) ([^\s]+)', message, re.I)
    if m:
        kind = "Secret" if m.group(1).lower() == "secret" else "ConfigMap"
        d.add(CRITICAL, f"{kind} '{m.group(2)}' is missing",
              f"Container '{name}' reads from the {kind} '{m.group(2)}', which doesn't exist in this namespace, so it can't be started.",
              evidence, [f"Create the {kind} '{m.group(2)}' in this namespace (or fix the name in the workload spec).",
                         "If it's managed by an operator (External Secrets, Sealed Secrets), check that operator's status."], name)
    elif k:
        d.add(CRITICAL, f"Key '{k.group(1)}' is missing from {k.group(2)} {k.group(3)}",
              f"Container '{name}' needs the key '{k.group(1)}', but the {k.group(2)} doesn't contain it.",
              evidence, [f"Add the key to the {k.group(2)}, or fix the key name in the workload spec."], name)
    elif "runasnonroot" in message.lower().replace(" ", "") or "non-root" in message.lower():
        d.add(CRITICAL, f"'{name}' must run as non-root but the image runs as root",
              "The pod sets `runAsNonRoot: true`, but the image's default user is root (or not numeric), so the kubelet refuses to start it.",
              evidence, ["Set `runAsUser` to a non-zero UID in the securityContext, or build the image with a numeric non-root USER."], name)
    else:
        d.add(CRITICAL, f"Container '{name}' has a configuration error",
              "The kubelet can't build this container's configuration (environment, volumes or security settings), so it never starts.",
              evidence, ["Read the exact error in the message above; it usually names the missing object."], name)


def _runtime_error(d, name, waiting_or_terminated, kind_label, extra_evidence=None):
    message = waiting_or_terminated.get("message") or ""
    low = message.lower()
    evidence = (extra_evidence or []) + ([f"Runtime: {_truncate(message, 400)}"] if message else [])
    if "exec format error" in low:
        d.add(CRITICAL, f"'{name}' image is built for the wrong CPU architecture",
              "The binary in the image can't run on this node's CPU (for example an arm64 image on an amd64 node, or the reverse).",
              evidence, ["Build a multi-arch image, or pin the pod to nodes of the matching `kubernetes.io/arch`."], name, key=f"crash/{name}")
    elif "executable file not found" in low or "no such file or directory" in low:
        d.add(CRITICAL, f"'{name}' command doesn't exist in the image",
              "The container's command or entrypoint points at a file that isn't in the image.",
              evidence, ["Check the `command`/`args` in the spec against the image's actual paths.",
                         "Check the image tag: a slimmer base image may not include the shell or binary."], name, key=f"crash/{name}")
    elif "permission denied" in low:
        d.add(CRITICAL, f"'{name}' command isn't executable",
              "The container's entrypoint exists but can't be executed (missing execute bit, or a read-only/noexec mount).",
              evidence, ["Make the entrypoint executable in the image (`chmod +x`), or check volume mounts that shadow it."], name, key=f"crash/{name}")
    else:
        d.add(CRITICAL, f"Container '{name}' fails to start",
              f"The container runtime couldn't start '{name}' ({kind_label}).",
              evidence, ["Read the runtime error above; it names the failing step."], name, key=f"crash/{name}")


def _crash(d, pod_ctx, c, cs, waiting, events, usage):
    """CrashLoopBackOff (or a failed run): explain why the last run ended."""
    name, now = c["name"], d.now
    last = ((cs.get("lastState") or {}).get("terminated") or (cs.get("state") or {}).get("terminated") or {})
    reason, code, signal = last.get("reason"), last.get("exitCode"), last.get("signal")
    started, finished = _parse_ts(last.get("startedAt")), _parse_ts(last.get("finishedAt"))
    ran = (finished - started).total_seconds() if started and finished else None
    restarts = cs.get("restartCount") or 0
    res = _resources(c)
    prefix = "Init container" if pod_ctx["isInit"] else "Container"

    evidence = []
    if code is not None or reason:
        evidence.append(f"Last exit: {reason or 'Error'}, exit code {code} ({exit_code_meaning(code, signal)})"
                        + (f", {_ago(finished, now)}" if finished else ""))
    if ran is not None:
        evidence.append(f"That run lasted {human_duration(ran)}")
    if restarts:
        evidence.append(f"Restarted {_plural(restarts, 'time')}")
    backoff = re.search(r"back-off (\S+) restarting", (waiting or {}).get("message") or "")
    if backoff:
        evidence.append(f"Kubernetes now waits {backoff.group(1)} between restarts (CrashLoopBackOff)")
    if last.get("message"):
        evidence.append(f"Termination message: {_truncate(last['message'], 300)}")

    liveness_fail = _probe_failures(events, "Liveness", name)
    startup_fail = _probe_failures(events, "Startup", name)
    killed_by_probe = _find_events(events, reason="Killing", contains="failed liveness probe", container=name) or \
        _find_events(events, reason="Killing", contains="failed startup probe", container=name)
    probe_kill = bool(liveness_fail or startup_fail or killed_by_probe)

    has_previous = bool((cs.get("lastState") or {}).get("terminated"))
    if has_previous:
        d.action("logs", f"Logs from the crashed run of '{name}' (--previous)", name, previous=True)
    d.action("logs", f"{'Current logs' if has_previous else 'Logs'} of '{name}'", name)
    last_words = f" Its last message was: “{_truncate(last['message'], 200)}”" if last.get("message") else ""

    looping = bool(waiting and waiting.get("reason") == "CrashLoopBackOff")
    verb = "keeps crashing" if looping else "crashed"

    if reason == "OOMKilled":
        limit = res["memoryLimitBytes"]
        peak = usage.get("peakMemoryBytes") if usage else None
        explanation = (f"{prefix} '{name}' used more memory than its limit of {format_bytes(limit)}, so the kernel killed it (OOMKilled)."
                       if limit else
                       f"{prefix} '{name}' was OOMKilled. It has no memory limit, so it was most likely killed when the node itself ran low on memory"
                       + (" (and the node is reporting memory pressure right now)." if pod_ctx.get("nodeMemoryPressure") else "."))
        if peak and limit:
            evidence.append(f"Recent peak usage (since Lex started): {format_bytes(peak)} of the {format_bytes(limit)} limit")
        fixes = ([f"Raise the memory limit above the real peak (currently {format_bytes(limit)})."] if limit else
                 ["Set a memory request and limit that match what the container really uses."]) + \
                ["Look for a memory leak or an unbounded cache if usage grows until it's killed.",
                 "For JVM, Node.js or Python workers, make sure heap settings fit inside the limit (e.g. -XX:MaxRAMPercentage, --max-old-space-size)."]
        d.add(CRITICAL, f"{prefix} '{name}' {verb}: out of memory", explanation, evidence, fixes, name, key=f"crash/{name}")
        return

    if probe_kill:
        probe_events = (liveness_fail or startup_fail)[:2] + killed_by_probe[:1]
        kind = "startup" if startup_fail and not liveness_fail else "liveness"
        probe = c.get("startupProbe" if kind == "startup" else "livenessProbe")
        evidence += [_event_evidence(e, now) for e in probe_events]
        if probe:
            evidence.append(f"{kind.capitalize()} probe: {describe_probe(probe)}")
        timeouts = any(("timeout" in e["message"].lower() or "deadline exceeded" in e["message"].lower()) for e in probe_events)
        fixes = []
        if timeouts and probe and (probe.get("timeoutSeconds") or 1) <= 1:
            fixes.append("The probe times out after just 1s: raise `timeoutSeconds`, or make the health endpoint cheaper.")
        if kind == "liveness" and not c.get("startupProbe"):
            fixes.append("If the app is slow to start, add a `startupProbe` so liveness checks only begin once it's up.")
        fixes += ["Check that the probe's port and path match what the app actually serves.",
                  "Read the logs from the killed run to see whether the app was hung or just slow."]
        d.add(CRITICAL, f"{prefix} '{name}' {verb}: its {kind} probe is failing",
              f"The app isn't answering its {kind} probe, so the kubelet kills and restarts it. "
              "The container itself may be fine but slow, hung, or listening on a different port or path.",
              evidence, fixes, name, key=f"crash/{name}")
        return

    if reason == "Completed" or code == 0:
        d.add(CRITICAL if looping else WARNING, f"{prefix} '{name}' exits immediately (successfully)",
              f"'{name}' finishes with exit code 0, but this pod's `restartPolicy` is `{pod_ctx['restartPolicy']}`, so Kubernetes restarts it, "
              "over and over. Long-running containers must keep their main process in the foreground.",
              evidence, ["If the process daemonizes (e.g. nginx without `daemon off;`), run it in the foreground.",
                         "If it's a one-off task, run it as a Job or CronJob instead of a Deployment.",
                         "Check that the command isn't missing its arguments (a shell that runs nothing exits 0)."], name, key=f"crash/{name}")
        return

    if reason == "StartError" or code in (126, 127, 128):
        _runtime_error(d, name, last, exit_code_meaning(code, signal), evidence)
        return

    if code in (137, 143) or signal in (9, 15):
        sig = "SIGKILL" if code == 137 or signal == 9 else "SIGTERM"
        d.add(CRITICAL if looping else WARNING, f"{prefix} '{name}' {verb}: killed by {sig}",
              f"Something outside the app stopped it with {sig}, and there's no OOM or probe failure on record. "
              + ("A SIGKILL usually means a node-level out-of-memory kill, or the process didn't exit within its termination grace period."
                 if sig == "SIGKILL" else "A SIGTERM usually means a deliberate stop: a rollout, a scale-down, a node drain, or the app's own supervisor."),
              evidence, ["Check the node's events for SystemOOM or memory pressure.",
                         "Check the logs from the killed run for what it was doing when it stopped."], name, key=f"crash/{name}")
        return

    if code in (134, 139) or signal in (6, 11):
        d.add(CRITICAL if looping else WARNING, f"{prefix} '{name}' {verb}: {'segmentation fault' if code == 139 or signal == 11 else 'aborted'}",
              "The process crashed in native code. This is a bug in the app or one of its native libraries, or a "
              "library built for a different platform (e.g. an Alpine/musl vs glibc mismatch).",
              evidence, ["Read the logs from the crashed run for a stack trace.",
                         "Check whether the crash started with a new image or base-image version."], name, key=f"crash/{name}")
        return

    # A plain application error: how quickly it died says a lot
    if ran is not None and ran < QUICK_CRASH_SECONDS:
        explanation = (f"'{name}' exits with code {code} ({exit_code_meaning(code, signal)}) only {human_duration(ran)} after starting. "
                       "Crashing this early is almost always a startup problem: bad or missing configuration, a missing environment "
                       "variable or secret, a wrong command-line flag, or a dependency (database, API, DNS) it can't reach.")
    elif ran is not None:
        explanation = (f"'{name}' ran for {human_duration(ran)} and then exited with code {code} ({exit_code_meaning(code, signal)}). "
                       "Since it got past startup, look for what changed at the end of that run: an unhandled error, a lost connection, or a fatal request.")
    else:
        explanation = f"'{name}' exits with code {code} ({exit_code_meaning(code, signal)})."
    if last_words:   # quoted in the explanation, so not repeated as evidence
        explanation += last_words
        evidence = [e for e in evidence if not e.startswith("Termination message:")]
    d.add(CRITICAL if looping else WARNING, f"{prefix} '{name}' {verb} (exit code {code})", explanation, evidence,
          ["Read the logs from the crashed run (`kubectl logs --previous`): the reason is usually in the last lines.",
           "Compare with the last working version: what changed in the image, config or secrets?"], name, key=f"crash/{name}")


def _container_creating(d, name, pod_age_s, events, init=False):
    mount = _find_events(events, reason="FailedMount")
    attach = _find_events(events, reason="FailedAttachVolume")
    sandbox = _find_events(events, reason="FailedCreatePodSandBox")
    if attach:
        multi = any("multi-attach" in e["message"].lower() for e in attach)
        d.add(CRITICAL, "A volume can't be attached" if not multi else "A volume is still attached to another node",
              ("The pod's volume (usually an EBS disk) is still attached to another node, typically one the previous pod ran on. "
               "ReadWriteOnce volumes can only attach to one node at a time." if multi else
               "The cloud provider failed to attach the pod's volume to this node."),
              [_event_evidence(e, d.now) for e in attach[:2]],
              ["Wait for the old node to release the volume (up to ~6 minutes after it goes away).",
               "Check that the old pod or node is really gone; a stuck Terminating pod keeps the volume attached.",
               "Check the volume's zone: it must be in the same availability zone as the node."], key="volume")
    elif mount:
        msg = " ".join(e["message"] for e in mount[:3]).lower()
        what = ("a Secret or ConfigMap it mounts doesn't exist" if ("secret" in msg or "configmap" in msg) and "not found" in msg
                else "its PersistentVolumeClaim isn't ready" if "persistentvolumeclaim" in msg
                else "a volume can't be mounted")
        d.add(CRITICAL, "A volume can't be mounted", f"The kubelet can't set up the pod's volumes: {what}. The containers can't start until it can.",
              [_event_evidence(e, d.now) for e in mount[:2]],
              ["Read the FailedMount event: it names the volume and the object it's waiting for.",
               "Create the missing Secret/ConfigMap, or check the PVC's status."], key="volume")
    elif sandbox:
        msg = " ".join(e["message"] for e in sandbox[:3]).lower()
        ip = any(s in msg for s in ("assign an ip", "ip address", "ipamd", "no available ip", "failed to allocate"))
        d.add(CRITICAL, "The pod network can't be set up" + (" (out of IP addresses)" if ip else ""),
              ("The CNI plugin couldn't give the pod an IP address. On EKS this usually means the subnet or the node's ENIs have run out of IPs."
               if ip else "The container runtime couldn't create the pod sandbox (its network namespace). This is a node or CNI problem, not an app problem."),
              [_event_evidence(e, d.now) for e in sandbox[:2]],
              (["Check free IPs in the node's subnet; enable prefix delegation or add subnets.",
                "Check the aws-node (VPC CNI) pods on this node."] if ip else
               ["Check the CNI pods (aws-node, calico, cilium) on this node.", "Check the node's kubelet and container runtime health."]), key="sandbox")
    elif init:
        if pod_age_s is not None and pod_age_s > STUCK_START_MINUTES * 60:
            d.add(WARNING, f"Stuck initializing for {human_duration(pod_age_s)}",
                  f"Init container '{name}' hasn't started, so neither have the app containers, and there's no error event explaining why. "
                  "The kubelet may be stuck setting up volumes or pulling the init image.",
                  [], ["Describe the pod and check the node's events for what the kubelet is doing."], name, key="creating")
            d.action("describe", "Describe the pod")
        elif pod_age_s is not None:
            d.add(INFO, "Still initializing", f"The pod was created {human_duration(pod_age_s)} ago and is setting up its init container '{name}'.",
                  [], [], name, key="creating")
            d.transient = d.transient or d.findings[-1]
    elif pod_age_s is not None and pod_age_s <= STUCK_START_MINUTES * 60:
        d.add(INFO, "Still starting", f"The pod was created {human_duration(pod_age_s)} ago and its containers are still being created.",
              [], [], key="creating")
        d.transient = d.transient or d.findings[-1]
    elif pod_age_s is not None:
        pulling = _find_events(events, reason="Pulling", container=name)
        d.add(WARNING, f"Containers have been creating for {human_duration(pod_age_s)}",
              "The containers haven't started yet and there's no error event explaining why. "
              + ("It's still pulling a (probably large) image." if pulling and not _find_events(events, reason="Pulled", container=name) else
                 "It may be waiting on a slow image pull or volume."),
              [_event_evidence(e, d.now) for e in pulling[:1]], ["Check the events below for the step it's stuck on."], key="creating")


def _nodes(count):
    """('1 node', 'is', 'has', "doesn't") / ('3 nodes', 'are', 'have', "don't") / ('Some nodes', ...)."""
    if count is None:
        return "Some nodes", "are", "have", "don't"
    return (_plural(count, "node"),) + (("is", "has", "doesn't") if count == 1 else ("are", "have", "don't"))


def _scheduling(d, pod, events, pod_age_s):
    spec = pod.get("spec") or {}
    cond = parse_cluster.get_pod_condition(pod, "PodScheduled") or {}
    message = cond.get("message") or ""
    sched_events = _find_events(events, reason="FailedScheduling")
    if not message and sched_events:
        message = sched_events[0]["message"]
    reason = cond.get("reason")
    waited = _ago(_parse_ts(cond.get("lastTransitionTime") or (pod.get("metadata") or {}).get("creationTimestamp")), d.now).replace(" ago", "")

    if reason == "SchedulingGated" or spec.get("schedulingGates"):
        gates = ", ".join(g.get("name", "?") for g in spec.get("schedulingGates") or [])
        d.add(WARNING, "Held back by a scheduling gate",
              f"The scheduler won't even consider this pod until something removes its scheduling gate{'s' if ',' in gates else ''}"
              f"{f' ({gates})' if gates else ''}. Gates are added on purpose by controllers such as rollout approvals or queueing systems (Kueue).",
              [f"Waiting for {waited}"], ["Find the controller that owns the gate and check why it hasn't released the pod."], key="sched")
        return

    req_mem, req_cpu = parse_cluster.effective_pod_request(spec, "memory"), parse_cluster.effective_pod_request(spec, "cpu")
    explanations, fixes = [], []
    head = re.match(r"\s*(\d+)/(\d+) nodes are available", message)
    body = message.split("preemption:")[0]
    body = re.sub(r"^\s*\d+/\d+ nodes are available:\s*", "", body).strip().rstrip(".")
    for part in [p.strip() for p in re.split(r",\s*(?=\d+\s)", body) if p.strip()]:
        m = re.match(r"(\d+)\s+(.*)", part)
        count, what = (int(m.group(1)), m.group(2)) if m else (None, part)
        n, is_, has, dont = _nodes(count)
        low = what.lower()
        if low.startswith("insufficient"):
            res = what.split(" ", 1)[1] if " " in what else "resources"
            amount = format_bytes(req_mem) if res == "memory" else format_cores(req_cpu) if res == "cpu" else None
            explanations.append(f"{n} {dont} have enough free {res} for its request of {amount}." if amount else f"{n} {dont} have enough free {res}.")
            fixes.append(f"Lower the pod's {res} request if it's oversized, or add capacity (a bigger node group, or let the autoscaler add nodes).")
        elif "node affinity/selector" in low or "node selector" in low:
            sel = ", ".join(f"{k}={v}" for k, v in (spec.get("nodeSelector") or {}).items())
            explanations.append(f"{n} {dont} match its node selector/affinity{f' ({sel})' if sel else ''}.")
            fixes.append("Check that some node actually has the labels the pod asks for (a typo or a retired instance type is common).")
        elif "untolerated taint" in low:
            taint = re.search(r"\{([^}]*)\}", what)
            explanations.append(f"{n} {has} a taint{f' ({taint.group(1)})' if taint else ''} the pod doesn't tolerate.")
            fixes.append("Add a matching toleration if the pod is meant to run there; otherwise these nodes are reserved for other work.")
        elif "were unschedulable" in low or "unschedulable" in low:
            explanations.append(f"{n} {is_} cordoned (marked unschedulable, e.g. being drained).")
        elif "anti-affinity" in low:
            explanations.append(f"{n} {is_} ruled out by its pod anti-affinity (already running a pod it must avoid).")
            fixes.append("With required anti-affinity, every replica needs its own node (or zone): add nodes or relax it to `preferred`.")
        elif "pod affinity" in low:
            explanations.append(f"{n} {dont} run the pods it must be co-located with (pod affinity).")
        elif "topology spread" in low:
            explanations.append(f"{n} would break its topology spread constraints.")
            fixes.append("Check `topologySpreadConstraints`: with `whenUnsatisfiable: DoNotSchedule` a missing zone blocks scheduling.")
        elif "volume node affinity" in low:
            explanations.append(f"{n} {is_} in a different zone from its volume (EBS volumes can't move between zones).")
            fixes.append("Make sure there's capacity in the volume's availability zone.")
        elif "unbound" in low and "persistentvolumeclaim" in low:
            explanations.append("Its PersistentVolumeClaim isn't bound to a volume yet.")
            fixes.append("Check the PVC's events and its StorageClass / provisioner (e.g. the EBS CSI driver).")
        elif "too many pods" in low:
            explanations.append(f"{n} {has} reached the maximum pod count.")
            fixes.append("Use larger nodes or raise max pods (on EKS, enable prefix delegation).")
        elif "free ports" in low:
            explanations.append(f"{n} already {'uses' if count == 1 else 'use'} the hostPort it needs.")
        elif "not-ready" in low or "not ready" in low:
            explanations.append(f"{n} {is_} not ready.")
        else:
            explanations.append(f"{n}: {what}.")

    scale_up = _find_events(events, reason="TriggeredScaleUp")
    no_scale = _find_events(events, reason="NotTriggerScaleUp")
    evidence = ([f"Scheduler: {_truncate(message, 400)}"] if message else []) + [f"Waiting for {waited}"]
    if scale_up:
        evidence.append(_event_evidence(scale_up[0], d.now))
    if no_scale:
        evidence.append(_event_evidence(no_scale[0], d.now))
        fixes.append("The cluster autoscaler says no node group could fit this pod: the request is bigger than any node type it can add, or the selector matches no group.")

    total = f" (none of the {head.group(2)} nodes fit)" if head and head.group(1) == "0" else ""
    if scale_up and not no_scale:
        d.add(INFO, "Waiting for a new node", "No current node can take the pod, but the cluster autoscaler is already adding one. This usually resolves within a few minutes.",
              evidence, [], key="sched")
        d.transient = d.findings[-1]
        return
    d.add(WARNING if (pod_age_s or 0) < 300 else CRITICAL, "Can't be scheduled" + total,
          " ".join(explanations) if explanations else "The scheduler can't find a node for this pod.",
          evidence, list(dict.fromkeys(fixes)), key="sched")


def _failed_pod(d, pod, events, containers):
    status = pod.get("status") or {}
    reason, message = status.get("reason"), status.get("message") or ""
    owner = parse_cluster.get_owner(pod.get("metadata") or {}) or {}
    replaced = owner.get("kind") in ("ReplicaSet", "StatefulSet", "DaemonSet")
    if reason == "Evicted":
        low = message.lower()
        what = ("memory" if "memory" in low else "disk space (ephemeral storage)" if "ephemeral" in low or "disk" in low or "nodefs" in low or "imagefs" in low
                else "process IDs" if "pid" in low else "a resource")
        own = "used more ephemeral storage than its limit" if "exceed" in low and "limit" in low else None
        d.add(WARNING, "Evicted by the kubelet" + (" (over its storage limit)" if own else f" (node low on {what.split(' (')[0]})"),
              (f"The pod {own}, so the kubelet evicted it." if own else
               f"The node ran low on {what}, so the kubelet evicted pods to protect itself, starting with pods using the most above their requests.")
              + (" Evicted pods aren't restarted; its controller created a replacement elsewhere." if replaced else " Evicted pods aren't restarted."),
              [f"Kubelet: {_truncate(message, 300)}"] if message else [],
              ["Set requests close to real usage so the pod isn't first in line for eviction.",
               "For disk evictions, look for logs or temp files written to the container filesystem; use an emptyDir with a sizeLimit.",
               "Clean up old Evicted pods: they hold no resources but clutter `kubectl get pods`."], key="pod-failed")
        return
    if reason in ("Shutdown", "NodeShutdown", "Terminated"):
        d.add(INFO, "Stopped because its node shut down",
              "The node was shut down (often a spot interruption or a scale-down) and the pod was stopped with it.",
              [f"Kubelet: {_truncate(message, 300)}"] if message else [], ["Nothing to fix on the pod itself; its controller replaces it."], key="pod-failed")
        return
    if reason and reason.startswith("OutOf"):
        d.add(WARNING, f"Rejected by the node ({reason})",
              "The kubelet refused to run the pod because the node didn't have the resources it requested when the pod arrived (a race with other pods).",
              [f"Kubelet: {_truncate(message, 300)}"] if message else [], ["Usually transient: its controller creates a replacement."], key="pod-failed")
        return
    if reason:
        d.add(WARNING, f"Pod failed ({reason})", message or "The pod failed and won't be restarted.",
              [], ["Check the pod's events below."], key="pod-failed")
        return
    # Failed through its containers (e.g. a Job pod): the container analysis carries the detail
    if not any(f["container"] for f in d.findings):
        d.add(WARNING, "Pod failed", "All containers have stopped and at least one failed. Pods with `restartPolicy: Never` aren't restarted.",
              [], ["Read the failed container's logs."], key="pod-failed")


def diagnose(pod, events=None, node=None, usage=None, now=None):
    """
    pod:    a full Pod object (as from `kubectl get pod -o json`)
    events: an Event List object or list of events for the pod (optional)
    node:   the pod's node from the compiled state ({conditions, unschedulable, ...}) (optional)
    usage:  {"memoryBytes", "peakMemoryBytes", "cpu", "peakCpu"} for the pod (optional)
    """
    now = now or datetime.datetime.now(datetime.timezone.utc)
    md, spec, status = pod.get("metadata") or {}, pod.get("spec") or {}, pod.get("status") or {}
    name, namespace = md.get("name"), md.get("namespace", "default")
    created = _parse_ts(md.get("creationTimestamp"))
    pod_age_s = (now - created).total_seconds() if created else None
    phase = status.get("phase") or "Unknown"
    detailed = parse_cluster.get_detailed_pod_status(pod)
    evs = normalize_events(events, md.get("uid"))
    d = Diagnosis(now)
    restart_policy = spec.get("restartPolicy") or "Always"

    # ---- node-level problems take priority: they explain everything else on the pod ----
    node_cond = (node or {}).get("conditions") or {}
    if node and node_cond.get("Ready") not in (None, "True"):
        d.add(CRITICAL, f"Its node {spec.get('nodeName')} is not ready",
              "The node this pod runs on isn't reporting as Ready, so the pod's status may be stale and its containers may not be running at all.",
              [f"Node Ready={node_cond.get('Ready')}" + (f" ({node_cond.get('ReadyReason')})" if node_cond.get("ReadyReason") else "")
               + (f": {_truncate(node_cond.get('ReadyMessage'), 200)}" if node_cond.get("ReadyMessage") else "")],
              ["Look at the node (press C on its building → events) before debugging the pod."], key="node")
    for cond_type, label in (("MemoryPressure", "memory"), ("DiskPressure", "disk"), ("PIDPressure", "process IDs")):
        if node_cond.get(cond_type) == "True":
            d.add(WARNING, f"Its node is low on {label}",
                  f"The node reports {cond_type}: the kubelet may evict pods from it, starting with those using the most above their requests.",
                  [node_cond.get(cond_type + "Message") or ""], [], key=f"node/{cond_type}", context=True)

    # ---- not scheduled ----
    if not spec.get("nodeName") and phase == "Pending":
        _scheduling(d, pod, evs, pod_age_s)

    # ---- stuck terminating ----
    # deletionTimestamp is when the grace period ends (deletion request + grace), not when deletion was requested
    deleting = _parse_ts(md.get("deletionTimestamp"))
    if deleting:
        grace = md.get("deletionGracePeriodSeconds")
        grace = grace if grace is not None else spec.get("terminationGracePeriodSeconds", 30)
        over = (now - deleting).total_seconds()
        if over > STUCK_TERMINATING_GRACE_SECONDS:
            finalizers = md.get("finalizers") or []
            d.add(CRITICAL if over > 600 else WARNING, f"Stuck terminating for {human_duration(over + grace)}",
                  ("It's been deleted, but " + (f"finalizers ({', '.join(finalizers)}) are still holding it: the controller responsible must remove them."
                   if finalizers else
                   "its node hasn't confirmed the containers stopped. That happens when the node is unreachable, or a container ignores SIGTERM and the runtime is stuck.")),
                  [f"Deletion requested {_ago(deleting - datetime.timedelta(seconds=grace), now)}; its {grace}s grace period ended {_ago(deleting, now)}"]
                  + ([f"Finalizers: {', '.join(finalizers)}"] if finalizers else []),
                  ["Check the node's health first.", "As a last resort, `kubectl delete pod --grace-period=0 --force` (only if the node is really gone)."], key="terminating")

    # ---- containers ----
    specs = {}
    for c in spec.get("initContainers") or []:
        specs[c.get("name")] = (c, "sidecar" if parse_cluster.is_restartable_init_container(c) else "init")
    for c in spec.get("containers") or []:
        specs[c.get("name")] = (c, "app")
    statuses = {cs.get("name"): cs for cs in (status.get("initContainerStatuses") or []) + (status.get("containerStatuses") or [])}
    for cs in status.get("initContainerStatuses") or []:
        specs.setdefault(cs.get("name"), ({"name": cs.get("name"), "image": cs.get("image")}, "init"))
    specs = dict(sorted(specs.items(), key=lambda kv: {"init": 0, "sidecar": 0, "app": 1}[kv[1][1]]))

    usage = usage or {}
    containers_out = []
    blocking_init_seen = False
    for cname, (c, kind) in specs.items():
        cs = statuses.get(cname) or {}
        state = cs.get("state") or {}
        waiting, running, terminated = state.get("waiting"), state.get("running"), state.get("terminated")
        running = running or {}
        is_running = "running" in state   # `running: {}` is a valid (empty) state
        last = (cs.get("lastState") or {}).get("terminated")
        res = _resources(c)
        ctx = {"isInit": kind == "init", "restartPolicy": restart_policy, "nodeMemoryPressure": node_cond.get("MemoryPressure") == "True"}

        if waiting:
            state_text = f"Waiting: {waiting.get('reason') or 'unknown'}"
        elif is_running:
            started = _parse_ts(running.get("startedAt"))
            state_text = f"Running{' for ' + human_duration((now - started).total_seconds()) if started else ''}"
        elif terminated:
            state_text = f"Terminated: {terminated.get('reason') or 'Error'} (exit {terminated.get('exitCode')})"
        else:
            state_text = "Not started"

        item = {
            "name": cname,
            "kind": kind,
            "image": c.get("image"),
            "state": state_text,
            "ready": bool(cs.get("ready")),
            "restarts": cs.get("restartCount") or 0,
            "requests": res["requests"],
            "limits": res["limits"],
            "probes": {k: describe_probe(c.get(k + "Probe")) for k in ("startup", "liveness", "readiness") if c.get(k + "Probe")},
        }
        t = last or terminated
        if t:
            item["lastTermination"] = {
                "reason": t.get("reason"), "exitCode": t.get("exitCode"),
                "meaning": exit_code_meaning(t.get("exitCode"), t.get("signal")),
                "finishedAt": t.get("finishedAt"),
                "ago": _ago(_parse_ts(t.get("finishedAt")), now) if t.get("finishedAt") else None,
            }
        containers_out.append(item)

        if not cs:
            continue
        wreason = (waiting or {}).get("reason") or ""

        # Init containers run in order: only the first unfinished one matters
        if kind == "init":
            if blocking_init_seen or (terminated and terminated.get("exitCode") == 0):
                continue
            blocking_init_seen = True

        if wreason == "CrashLoopBackOff" or (terminated and terminated.get("exitCode") not in (0, None) and
                                             (kind == "init" or phase == "Failed" or restart_policy != "Always")):
            _crash(d, ctx, c, cs, waiting, evs, usage)
        elif wreason in ("ImagePullBackOff", "ErrImagePull", "InvalidImageName", "ErrImageNeverPull"):
            _image_pull(d, cname, c.get("image"), waiting, evs)
        elif wreason == "CreateContainerConfigError":
            _config_error(d, cname, waiting)
        elif wreason in ("CreateContainerError", "RunContainerError", "StartError"):
            _runtime_error(d, cname, waiting, wreason)
            if last:
                d.findings[-1]["evidence"].append(f"Last exit: {last.get('reason')}, exit code {last.get('exitCode')}")
        elif wreason == "ContainerCreating":
            _container_creating(d, cname, pod_age_s, evs)
        elif kind == "init" and wreason == "PodInitializing":
            _container_creating(d, cname, pod_age_s, evs, init=True)
        elif kind == "init" and is_running:
            started = _parse_ts(running.get("startedAt"))
            run_s = (now - started).total_seconds() if started else None
            if run_s is not None and run_s > SLOW_CREATE_MINUTES * 60:
                d.add(WARNING, f"Waiting on init container '{cname}' for {human_duration(run_s)}",
                      f"The app containers can't start until init container '{cname}' finishes, and it's still running. "
                      "Init containers often wait for a dependency (a database, a migration, a service) that isn't available.",
                      [], ["Read the init container's logs to see what it's waiting for."], cname, key=f"init/{cname}")
                d.action("logs", f"Logs of init container '{cname}'", cname)
        elif is_running and kind in ("app", "sidecar") and not cs.get("ready") and phase == "Running" and not deleting:
            ready_fail = _probe_failures(evs, "Readiness", cname)
            startup_fail = _probe_failures(evs, "Startup", cname)
            started = _parse_ts(running.get("startedAt"))
            up_s = (now - started).total_seconds() if started else None
            if ready_fail or startup_fail:
                kind_label = "startup" if startup_fail and not ready_fail else "readiness"
                probe = c.get(kind_label + "Probe")
                timeouts = any("timeout" in e["message"].lower() or "deadline" in e["message"].lower() for e in ready_fail + startup_fail)
                d.add(WARNING, f"'{cname}' is running but not ready: its {kind_label} probe is failing",
                      f"The container is up, but it isn't passing its {kind_label} probe, so Services don't send it traffic"
                      + (" (and the rollout can't progress)." if kind_label == "readiness" else ". If it keeps failing, the kubelet will restart it."),
                      [_event_evidence(e, now) for e in (ready_fail or startup_fail)[:2]]
                      + ([f"{kind_label.capitalize()} probe: {describe_probe(probe)}"] if probe else []),
                      (["The probe times out after 1s: raise `timeoutSeconds` or make the endpoint cheaper."] if timeouts and probe and (probe.get("timeoutSeconds") or 1) <= 1 else [])
                      + ["Check that the probe's port and path match what the app serves.",
                         "If readiness checks a dependency (database, downstream API), that dependency may be the real problem."], cname, key=f"ready/{cname}")
                d.action("logs", f"Current logs of '{cname}'", cname)
            elif up_s is not None and up_s < 120:
                d.add(INFO, f"'{cname}' is still starting up", f"It started {human_duration(up_s)} ago and hasn't reported ready yet.", [], [], cname)
                d.transient = d.transient or d.findings[-1]
            else:
                d.add(WARNING, f"'{cname}' is running but not ready",
                      "The container is up but not ready, so it gets no traffic. There's no recent probe failure on record (events expire after about an hour).",
                      [], ["Describe the pod to see the readiness probe and its latest result."], cname, key=f"ready/{cname}")
                d.action("describe", "Describe the pod")

        # Recovered restarts: the container is up now, but it has been restarting
        if is_running and last and not wreason and (cs.get("restartCount") or 0) > 0:
            finished = _parse_ts(last.get("finishedAt"))
            recent = finished and (now - finished).total_seconds() < RECENT_RESTART_MINUTES * 60
            lreason, lcode = last.get("reason") or "Error", last.get("exitCode")
            if lreason == "OOMKilled":
                limit = res["memoryLimitBytes"]
                peak = usage.get("peakMemoryBytes")
                d.add(WARNING if recent else INFO, f"'{cname}' was OOMKilled {_ago(finished, now)}",
                      ("It's running again now, but its last run ended because it used more memory than its limit of "
                       f"{format_bytes(limit)}." if limit else
                       "It's running again now, but its last run was OOMKilled. It has no memory limit, so the node itself most likely ran low on memory"
                       + (" (and the node is reporting memory pressure right now)." if ctx["nodeMemoryPressure"] else "."))
                      + f" It has restarted {_plural(cs.get('restartCount'), 'time')} in total.",
                      [f"Last exit: OOMKilled, exit code {lcode}"]
                      + ([f"Recent peak usage (since Lex started): {format_bytes(peak)}" + (f" of the {format_bytes(limit)} limit" if limit else "")] if peak else []),
                      [f"Raise the memory limit above {format_bytes(limit)}, or find what makes memory spike." if limit else
                       "Set a memory request and limit that match what it really uses, so it isn't killed by node-level OOM."], cname, key=f"restart/{cname}")
            else:
                d.add(WARNING if recent else INFO, f"'{cname}' restarted {_plural(cs.get('restartCount'), 'time')}",
                      f"It's running now. Its last run ended {_ago(finished, now)} with {lreason}, exit code {lcode} ({exit_code_meaning(lcode, last.get('signal'))}).",
                      [], ["Read the logs from the previous run to see why it stopped."], cname, key=f"restart/{cname}")
            d.action("logs", f"Logs from the previous run of '{cname}' (--previous)", cname, previous=True)

        # Running close to the memory limit (needs usage)
        limit = res["memoryLimitBytes"]
        peak = usage.get("peakMemoryBytes")
        if is_running and limit and peak and len(spec.get("containers") or []) == 1 and kind == "app" and peak >= limit * MEMORY_NEAR_LIMIT:
            d.add(WARNING, f"'{cname}' is close to its memory limit",
                  f"Its recent peak was {format_bytes(peak)}, {round(peak / limit * 100)}% of the {format_bytes(limit)} limit. One more spike and it gets OOMKilled.",
                  [], ["Raise the memory limit, or reduce the app's memory use."], cname, key=f"nearlimit/{cname}")

    # ---- pod-level failures ----
    if phase == "Failed":
        _failed_pod(d, pod, evs, containers_out)

    # ---- hygiene: not the cause of a failure, but worth knowing ----
    is_job = (parse_cluster.get_owner(md) or {}).get("kind") == "Job" or restart_policy != "Always"
    if not is_job:
        for c in spec.get("containers") or []:
            if not c.get("readinessProbe"):
                d.add(INFO, f"'{c.get('name')}' has no readiness probe",
                      "Without one, Kubernetes sends it traffic as soon as the process starts, even before it's able to serve, and rollouts can't tell a broken version from a working one.",
                      [], ["Add a readinessProbe that checks the app can actually serve requests."], c.get("name"), key=f"noready/{c.get('name')}")
    for c in spec.get("containers") or []:
        res = _resources(c)
        if not res["limits"].get("memory"):
            d.add(INFO, f"'{c.get('name')}' has no memory limit",
                  "It can use as much memory as the node has. A leak then turns into node memory pressure, and evictions of other pods, instead of a clean OOM kill of this one.",
                  [], ["Set a memory limit (at or a bit above its request)."], c.get("name"), key=f"nolimit/{c.get('name')}")

    # ---- verdict ----
    d.findings.sort(key=lambda f: (SEVERITY_RANK[f["severity"]], f["_context"]))
    problems = [f for f in d.findings if f["severity"] in (CRITICAL, WARNING)]
    if problems:
        top = problems[0]
        severity = top["severity"]
        headline = top["title"]
        summary = top["explanation"]
        if len(problems) > 1:
            summary += f" ({_plural(len(problems) - 1, 'other problem')} below.)"
    elif phase == "Succeeded":
        severity, headline, summary = "ok", "Completed successfully", "All containers ran to completion with exit code 0."
    elif d.transient:
        severity, headline, summary = INFO, d.transient["title"], d.transient["explanation"]
    else:
        severity = "ok"
        headline = "No problems found"
        summary = ("The pod is running and all its containers are ready." if phase == "Running"
                   else f"Nothing looks wrong (phase {phase}).")

    warnings = [e for e in evs if e["type"] == "Warning"]
    shown_events = (warnings + [e for e in evs if e["type"] != "Warning"])[:MAX_EVENTS]
    if any(f["severity"] in (CRITICAL, WARNING) for f in d.findings):
        d.action("events", "All events for this pod")
        d.action("describe", "Describe the pod")

    for f in d.findings:
        f.pop("_key", None)
        f.pop("_context", None)
    for e in shown_events:
        e.pop("_when", None)

    return {
        "pod": {
            "name": name, "namespace": namespace, "node": spec.get("nodeName"), "phase": phase, "status": detailed,
            "age": human_duration(pod_age_s) if pod_age_s is not None else None,
            "restarts": sum((cs.get("restartCount") or 0) for cs in statuses.values()),
            "owner": parse_cluster.resolve_workload(md),
            "restartPolicy": restart_policy,
        },
        "severity": severity,
        "headline": headline,
        "summary": summary,
        "findings": d.findings,
        "containers": containers_out,
        "events": shown_events,
        "eventCount": len(evs),
        "actions": d.actions,
        "usageWindowMinutes": usage.get("windowMinutes") if usage else None,
        "generatedAt": _iso(now),
    }


# ---------- demo support ----------

def synthesize_demo_events(pod, now=None):
    """Plausible events for the demo fixtures, derived from the pod's own status."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    md, spec, status = pod.get("metadata") or {}, pod.get("spec") or {}, pod.get("status") or {}
    name = md.get("name")

    def ev(type_, reason, message, minutes_ago, container=None, count=1):
        return {"type": type_, "reason": reason, "message": message, "count": count,
                "lastTimestamp": _iso(now - datetime.timedelta(minutes=minutes_ago)),
                "involvedObject": {"kind": "Pod", "name": name, "fieldPath": f"spec.containers{{{container}}}" if container else None},
                "source": {"component": "kubelet" if container else "default-scheduler"}}

    items = []
    if not spec.get("nodeName"):
        cond = parse_cluster.get_pod_condition(pod, "PodScheduled") or {}
        if cond.get("reason") == "Unschedulable":
            items.append(ev("Warning", "FailedScheduling", cond.get("message") or "", 1, count=12))
            items.append(ev("Normal", "NotTriggerScaleUp",
                            "pod didn't trigger scale-up: 1 max node group size reached", 2, count=8))
        return {"items": items}
    items.append(ev("Normal", "Scheduled", f"Successfully assigned {md.get('namespace')}/{name} to {spec.get('nodeName')}", 60))
    for c in spec.get("containers") or []:
        items.append(ev("Normal", "Pulled", f'Container image "{c.get("image")}" already present on machine', 59, c.get("name")))
    for cs in status.get("containerStatuses") or []:
        cname = cs.get("name")
        waiting = (cs.get("state") or {}).get("waiting") or {}
        last = (cs.get("lastState") or {}).get("terminated") or {}
        if waiting.get("reason") == "CrashLoopBackOff":
            items.append(ev("Warning", "BackOff", f"Back-off restarting failed container {cname} in pod {name}", 1, cname, count=cs.get("restartCount") or 1))
        if last.get("reason") == "OOMKilled":
            items.append(ev("Normal", "Pulled", f"Container image already present on machine", 5, cname))
            items.append(ev("Normal", "Started", f"Started container {cname}", 5, cname, count=cs.get("restartCount") or 1))
    return {"items": items}


def demo_previous_logs(pod, container=None):
    """`kubectl logs --previous` for the demo fixtures: the crashed run of the demo's crash-looping cache."""
    if pod.startswith("cache-pod"):
        return ("1:C 03 Oct 2026 04:01:12.004 * oO0OoO0OoO0Oo Redis is starting oO0OoO0OoO0Oo\n"
                "1:C 03 Oct 2026 04:01:12.004 * Redis version=7.2.4, bits=64, commit=00000000, modified=0, pid=1, just started\n"
                "1:C 03 Oct 2026 04:01:12.005 * Configuration loaded\n\n"
                "*** FATAL CONFIG FILE ERROR (Redis 7.2.4) ***\n"
                "Reading the configuration file, at line 12\n"
                ">>> 'maxmemory 4gb-x'\n"
                "Bad directive or wrong number of arguments\n")
    if pod.startswith("analytics-worker"):
        return ("2026-10-03T03:41:02Z INFO  worker: loading feature matrix (rows=48,000,000)\n"
                "2026-10-03T03:52:40Z INFO  worker: join stage 3/5, resident=14.8GiB\n"
                "2026-10-03T03:59:41Z WARN  worker: resident=15.9GiB, approaching container limit\n"
                "(output ends here: the process was killed without a chance to log)\n")
    return f"[DEMO MODE ACTIVE] No previous run on record for '{pod}'" + (f" container '{container}'" if container else "") + \
        " (the container hasn't restarted).\n"
