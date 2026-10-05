#!/usr/bin/env python3
"""
Security posture findings for the X-ray lens.

Each pod gets a compact summary: its findings (severity + code + short detail) and its worst severity.
Findings are posture, not incidents, so they live beside alerts rather than in them.
"""

import redaction

CRITICAL, HIGH, MEDIUM, LOW = "critical", "high", "medium", "low"
SEVERITY_RANK = {CRITICAL: 4, HIGH: 3, MEDIUM: 2, LOW: 1}

DANGEROUS_CAPABILITIES = {"SYS_ADMIN", "SYS_PTRACE", "SYS_MODULE", "DAC_READ_SEARCH", "ALL"}
ELEVATED_CAPABILITIES = {"NET_ADMIN", "NET_RAW", "SYS_RESOURCE", "SYS_TIME", "BPF", "PERFMON"}
RUNTIME_SOCKETS = ("/var/run/docker.sock", "/run/docker.sock", "/var/run/containerd/containerd.sock",
                   "/run/containerd/containerd.sock", "/var/run/crio/crio.sock", "/run/crio/crio.sock")
SENSITIVE_HOST_PATHS = ("/", "/etc", "/root", "/var/lib/kubelet", "/proc", "/sys", "/home")
PLATFORM_NAMESPACES = ("kube-system", "kube-public", "kube-node-lease")
# Init containers injected by service meshes need NET_ADMIN/NET_RAW (and root) to program iptables
MESH_INIT_CONTAINERS = {"istio-init", "istio-validation", "linkerd-init", "linkerd2-proxy-init", "kuma-init"}
PLATFORM_NAMESPACE_PREFIXES = ("amazon-", "aws-", "calico", "tigera", "cilium", "istio-", "linkerd",
                               "cert-manager", "karpenter", "gatekeeper", "kyverno")

# What each finding means, for the UI legend and the Security tab
FINDING_INFO = {
    "privileged": (CRITICAL, "Privileged container"),
    "host-pid": (CRITICAL, "Shares host PID namespace"),
    "host-ipc": (CRITICAL, "Shares host IPC namespace"),
    "runtime-socket": (CRITICAL, "Mounts the container runtime socket"),
    "dangerous-capability": (CRITICAL, "Dangerous Linux capability"),
    "sensitive-host-path": (CRITICAL, "Mounts a sensitive host path"),
    "host-network": (HIGH, "Uses the host network"),
    "host-path": (HIGH, "Mounts a host path"),
    "secret-env": (HIGH, "Secret in a plain env var"),
    "elevated-capability": (HIGH, "Elevated Linux capability"),
    "run-as-root": (MEDIUM, "May run as root"),
    "default-sa-token": (MEDIUM, "Default service account token mounted"),
    "mutable-image": (LOW, "Mutable image tag (:latest or none)"),
    # F-21: network exposure (network.py decides which applies; at most one per pod)
    "public-no-netpol": (HIGH, "Reachable from outside the cluster, no NetworkPolicy restricts it"),
    "exposed-no-netpol": (MEDIUM, "Exposed via internal load balancer or NodePort, no NetworkPolicy restricts it"),
    "no-netpol": (LOW, "Not covered by any ingress NetworkPolicy"),
}


def is_platform_workload(namespace, workload):
    """Expected system agents (CNI, CSI, log shippers...) that legitimately need host access."""
    ns = namespace or ""
    if ns in PLATFORM_NAMESPACES or ns.startswith(PLATFORM_NAMESPACE_PREFIXES):
        return True
    return bool(workload) and workload.get("kind") == "DaemonSet"


def _add(findings, code, detail=""):
    if any(f["code"] == code and f.get("detail") == detail for f in findings):
        return
    severity, _ = FINDING_INFO[code]
    findings.append({"severity": severity, "code": code, "detail": detail})


def pod_findings(pod, network_finding=None):
    """Findings for one raw pod object. Env values may already be redacted; names are enough.
    network_finding is (code, detail) from network.analyze(), when Services and NetworkPolicies were listed."""
    spec = pod.get("spec") or {}
    pod_sc = spec.get("securityContext") or {}
    findings = []

    if spec.get("hostPID"):
        _add(findings, "host-pid")
    if spec.get("hostIPC"):
        _add(findings, "host-ipc")
    if spec.get("hostNetwork"):
        _add(findings, "host-network")

    for v in spec.get("volumes") or []:
        host_path = (v.get("hostPath") or {}).get("path")
        if not host_path:
            continue
        path = host_path.rstrip("/") or "/"
        if path in RUNTIME_SOCKETS:
            _add(findings, "runtime-socket", path)
        elif path in SENSITIVE_HOST_PATHS:
            _add(findings, "sensitive-host-path", path)
        else:
            _add(findings, "host-path", path)

    sa = spec.get("serviceAccountName") or spec.get("serviceAccount") or "default"
    if sa == "default" and spec.get("automountServiceAccountToken", True) is not False:
        _add(findings, "default-sa-token")

    for key in ("initContainers", "containers"):
        for c in spec.get(key) or []:
            sc = c.get("securityContext") or {}
            name = c.get("name") or "?"
            mesh_init = key == "initContainers" and name in MESH_INIT_CONTAINERS
            if sc.get("privileged"):
                _add(findings, "privileged", name)
            caps = {str(x).upper() for x in ((sc.get("capabilities") or {}).get("add") or [])}
            for cap in sorted(caps & DANGEROUS_CAPABILITIES):
                _add(findings, "dangerous-capability", f"{name}: {cap}")
            for cap in sorted(caps & ELEVATED_CAPABILITIES):
                if not mesh_init:
                    _add(findings, "elevated-capability", f"{name}: {cap}")
            run_as_non_root = sc.get("runAsNonRoot", pod_sc.get("runAsNonRoot"))
            run_as_user = sc.get("runAsUser", pod_sc.get("runAsUser"))
            if not run_as_non_root and run_as_user in (None, 0) and not mesh_init:
                _add(findings, "run-as-root", name)
            image = c.get("image") or ""
            last = image.split("/")[-1]
            if "@sha256:" not in image and (last.endswith(":latest") or ":" not in last):
                _add(findings, "mutable-image", image)
            for env in c.get("env") or []:
                if "value" not in env or env.get("valueFrom"):
                    continue
                value = str(env.get("value") or "")
                if value == redaction.REDACTED_CREDENTIAL_URL or redaction.has_credential_url(value):
                    _add(findings, "secret-env", f"{env.get('name')} (credentials in URL)")
                elif redaction.looks_like_secret(env.get("name"), value):
                    _add(findings, "secret-env", env.get("name"))

    if network_finding:
        _add(findings, network_finding[0], network_finding[1])

    findings.sort(key=lambda f: (-SEVERITY_RANK[f["severity"]], f["code"], f.get("detail") or ""))
    return findings


def summarize(pod, namespace, workload, network_finding=None):
    findings = pod_findings(pod, network_finding)
    worst = findings[0]["severity"] if findings else None
    return {
        "risk": worst,
        "platform": is_platform_workload(namespace, workload),
        "findings": findings,
        "unpinnedImages": sum(1 for key in ("initContainers", "containers") for c in ((pod.get("spec") or {}).get(key) or [])
                              if "@sha256:" not in (c.get("image") or "")),
    }
