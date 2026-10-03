#!/usr/bin/env python3
import os
import sys
import json
import re
import subprocess
import hashlib
import tempfile
import datetime
import alerts
import redaction
import security

# Configuration
APP_DIR = os.path.dirname(os.path.abspath(__file__))
# All cluster dumps, compiled states and the DVR database live here. This directory is never
# served over HTTP and is git-ignored. Override with LEX_DATA_DIR.
DATA_DIR = os.environ.get("LEX_DATA_DIR") or os.path.join(APP_DIR, "data")
NODES_RAW_FILE = "raw-nodes.json"
PODS_RAW_FILE = "raw-pods.json"
OUTPUT_FILE = "cluster_state.json"

# kubectl timeouts: the API server-side request timeout, plus a hard cap on the subprocess itself
KUBECTL_REQUEST_TIMEOUT = "30s"
KUBECTL_PROCESS_TIMEOUT = 60

def ensure_data_dir():
    # Owner-only: dumps describe the cluster in detail (secret values are redacted, but still)
    os.makedirs(DATA_DIR, mode=0o700, exist_ok=True)
    try:
        os.chmod(DATA_DIR, 0o700)
    except OSError:
        pass

def get_file_paths(context=None):
    """Returns context-specific file paths (inside DATA_DIR) to support concurrent multi-cluster scraping."""
    if not context or context == "demo":
        names = (NODES_RAW_FILE, PODS_RAW_FILE, OUTPUT_FILE)
    else:
        # Clean the context name to ensure safe local filesystem naming
        safe_ctx = re.sub(r'[^a-zA-Z0-9_-]', '_', context)
        names = (f"raw-nodes-{safe_ctx}.json", f"raw-pods-{safe_ctx}.json", f"cluster_state-{safe_ctx}.json")
    return tuple(os.path.join(DATA_DIR, n) for n in names)

def get_workloads_path(context=None):
    """Raw Deployments/StatefulSets/DaemonSets dump for a context (inside DATA_DIR)."""
    if not context or context == "demo":
        return os.path.join(DATA_DIR, "raw-workloads.json")
    safe_ctx = re.sub(r'[^a-zA-Z0-9_-]', '_', context)
    return os.path.join(DATA_DIR, f"raw-workloads-{safe_ctx}.json")

def write_file_atomic(path, text):
    """Writes text to path via a temp file + rename so readers never see a partially written file."""
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

# Sleek, premium modern palette for namespace colors (to avoid basic primary colors)
NAMESPACE_COLORS = [
    0x3b82f6,  # Indigo/Sleek Blue
    0x10b981,  # Emerald Green
    0x8b5cf6,  # Purple/Violet
    0xf59e0b,  # Warm Amber
    0x06b6d4,  # Vivid Cyan
    0x14b8a6,  # Modern Teal
    0x6366f1,  # Electric Indigo
    0xa855f7,  # Deep Purple
    0xec4899,  # Electric Fuchsia/Pink
    0xf97316,  # Vivid Orange
    0x0ea5e9,  # Vivid Sky Blue
    0x84cc16,  # Cyber Lime
    0xeab308,  # Golden Yellow
    0x475569,  # Slate Gray
    0x0284c7,  # Deep Sky
    0x0d9488,  # Dark Teal
]

def fnv1a_32(text):
    """32-bit FNV-1a hash over UTF-16 code units (matches the JS implementation in index.html)."""
    h = 0x811c9dc5
    data = text.encode('utf-16-le')
    for i in range(0, len(data), 2):
        h ^= data[i] | (data[i + 1] << 8)
        h = (h * 0x01000193) & 0xffffffff
    return h

def get_namespace_color(namespace):
    """Generates a stable palette color for a namespace (same algorithm as the frontend)."""
    return NAMESPACE_COLORS[fnv1a_32(namespace) % len(NAMESPACE_COLORS)]

def round_precise(val):
    """Rounding for values written to the state file: keeps small pods (e.g. 32Mi) from collapsing to 0."""
    return round(val, 4)

# Kubernetes resource.Quantity suffixes (case-sensitive: "m" is milli, "M" is mega)
QUANTITY_SUFFIXES = {
    'Ki': 1024, 'Mi': 1024**2, 'Gi': 1024**3, 'Ti': 1024**4, 'Pi': 1024**5, 'Ei': 1024**6,
    'n': 1e-9, 'u': 1e-6, 'm': 1e-3, '': 1,
    'k': 1e3, 'M': 1e6, 'G': 1e9, 'T': 1e12, 'P': 1e15, 'E': 1e18,
}
QUANTITY_RE = re.compile(r'^([+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+))(?:[eE]([+-]?[0-9]+))?(Ki|Mi|Gi|Ti|Pi|Ei|n|u|m|k|M|G|T|P|E)?$')

def parse_quantity(value):
    """Parses a Kubernetes quantity string (e.g. '500m', '64Mi', '1.5G', '1e3') to a float in base units."""
    if value is None or value == "":
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    match = QUANTITY_RE.match(str(value).strip())
    if not match:
        return 0.0
    number, exponent, suffix = match.groups()
    result = float(number) * QUANTITY_SUFFIXES[suffix or '']
    if exponent:
        result *= 10 ** int(exponent)
    return result

def parse_cpu_to_cores(cpu_str):
    """Parses a Kubernetes CPU quantity (e.g. '500m', '2') to unrounded floating-point cores."""
    return parse_quantity(cpu_str)

def parse_memory_to_gb(mem_str):
    """Parses a Kubernetes memory quantity (e.g. '24Gi', '32551416Ki', '16G') to unrounded GiB."""
    return parse_quantity(mem_str) / (1024**3)

_kubectl_available = None

def kubectl_available():
    """Checks once per process whether kubectl is installed."""
    global _kubectl_available
    if _kubectl_available is None:
        try:
            subprocess.run(["kubectl", "version", "--client"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True, timeout=10)
            _kubectl_available = True
        except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
            _kubectl_available = False
    return _kubectl_available

def run_kubectl(context=None):
    """Attempts to run kubectl to fetch live cluster JSONs."""
    nodes_file, pods_file, _ = get_file_paths(context)
    if context:
        print(f"Attempting to query Kubernetes cluster context: {context}...")
    else:
        print("Attempting to query active Kubernetes cluster context...")
        
    if not kubectl_available():
        print("▲ Note: 'kubectl' CLI utility is not installed or not in system PATH.")
        return False

    base_cmd = ["kubectl"]
    if context:
        base_cmd += [f"--context={context}"]
    else:
        try:
            subprocess.run(["kubectl", "config", "current-context"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True, timeout=10)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            print("▲ Note: 'kubectl' is installed, but no active cluster context was detected.")
            return False
    # No separate connectivity pre-check: the 'get' calls below carry their own timeouts, and a short
    # cluster-info probe produced false "unreachable" results on EKS when exec-plugin auth was slow.

    get_cmd = base_cmd + [f"--request-timeout={KUBECTL_REQUEST_TIMEOUT}", "get"]

    # Fetch nodes
    try:
        print(f"Fetching nodes list and writing to {nodes_file}...")
        nodes_res = subprocess.run(get_cmd + ["nodes", "-o", "json"], capture_output=True, text=True, check=True, timeout=KUBECTL_PROCESS_TIMEOUT)
        write_file_atomic(nodes_file, redaction.sanitize_json_text_for_disk(nodes_res.stdout))
    except subprocess.CalledProcessError as e:
        print(f"▲ Error fetching nodes: {e.stderr}")
        return False
    except subprocess.TimeoutExpired:
        print(f"▲ Error fetching nodes: timed out after {KUBECTL_PROCESS_TIMEOUT}s")
        return False

    # Fetch pods
    try:
        print(f"Fetching pods list across all namespaces and writing to {pods_file}...")
        pods_res = subprocess.run(get_cmd + ["pods", "--all-namespaces", "-o", "json"], capture_output=True, text=True, check=True, timeout=KUBECTL_PROCESS_TIMEOUT)
        # Literal env values (often credentials) and last-applied-configuration never reach the disk
        write_file_atomic(pods_file, redaction.sanitize_json_text_for_disk(pods_res.stdout))
    except subprocess.CalledProcessError as e:
        print(f"▲ Error fetching pods: {e.stderr}")
        return False
    except subprocess.TimeoutExpired:
        print(f"▲ Error fetching pods: timed out after {KUBECTL_PROCESS_TIMEOUT}s")
        return False

    # Workloads (for replica availability). Non-fatal: RBAC may not allow it, and the rest of Lex still works.
    workloads_file = get_workloads_path(context)
    try:
        print(f"Fetching deployments/statefulsets/daemonsets/poddisruptionbudgets and writing to {workloads_file}...")
        wl_res = subprocess.run(get_cmd + ["deployments,statefulsets,daemonsets,poddisruptionbudgets", "--all-namespaces", "-o", "json"],
                                capture_output=True, text=True, check=True, timeout=KUBECTL_PROCESS_TIMEOUT)
        write_file_atomic(workloads_file, redaction.sanitize_json_text_for_disk(wl_res.stdout))
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        detail = getattr(e, "stderr", "") or str(e)
        print(f"▲ Could not fetch workloads (continuing without them): {str(detail).strip()[:200]}")
        write_file_atomic(workloads_file, json.dumps({"items": [], "lexError": str(detail).strip()[:500]}))

    print("● Live cluster data successfully extracted!")
    return True

def get_cluster_name(context=None):
    """Attempts to fetch active Kubernetes context/cluster name."""
    if context:
        return context
    try:
        res = subprocess.run(["kubectl", "config", "current-context"], capture_output=True, text=True, check=True, timeout=10)
        return res.stdout.strip()
    except Exception:
        return None

MOCK_MARKER = "lex.dev/generated-mock"

def is_generated_mock(path):
    """True if path is a fixture produced by create_mock_files_if_missing (safe to regenerate)."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f).get("metadata", {}).get(MOCK_MARKER) is True
    except (OSError, ValueError, AttributeError):
        return False

def create_mock_files_if_missing(context=None):
    """Generates standard mock JSON files if they don't exist in the directory."""
    nodes_file, pods_file, _ = get_file_paths(context)
    if os.path.exists(nodes_file) and os.path.exists(pods_file) and not (is_generated_mock(nodes_file) and is_generated_mock(pods_file)):
        # Hand-written fixtures are left untouched
        return

    print("▲ Generating default mock files (timestamps relative to now)...")
    
    # Generate dynamic creation timestamps for stuck and normal init pods relative to current run time
    now = datetime.datetime.now(datetime.timezone.utc)
    stuck_time = (now - datetime.timedelta(minutes=45)).strftime("%Y-%m-%dT%H:%M:%SZ")
    normal_time = (now - datetime.timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    joining_time = (now - datetime.timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    
    mock_nodes = {
        "apiVersion": "v1",
        "kind": "List",
        "metadata": {MOCK_MARKER: True},
        "items": [
            {
                "metadata": {
                    "name": "node-alpha",
                    "creationTimestamp": "2026-05-15T00:00:00Z",
                    "labels": {
                        "kubernetes.io/hostname": "node-alpha",
                        "kubernetes.io/os": "linux",
                        "kubernetes.io/arch": "amd64",
                        "node.kubernetes.io/instance-type": "t3.large",
                        "environment": "production",
                        "karpenter.sh/capacity-type": "spot",
                        "topology.kubernetes.io/region": "us-east-1",
                        "topology.kubernetes.io/zone": "us-east-1a"
                    }
                },
                "spec": {
                    "providerID": "aws:///us-east-1a/i-0a1b2c3d4e5f60001"
                },
                "status": {
                    "allocatable": {"memory": "24Gi", "cpu": "8"},
                    "capacity": {"memory": "24Gi", "cpu": "8"},
                    "conditions": [
                        {"type": "Ready", "status": "True"},
                        {"type": "MemoryPressure", "status": "False"},
                        {"type": "DiskPressure", "status": "False"},
                        {"type": "PIDPressure", "status": "False"}
                    ]
                }
            },
            {
                "metadata": {
                    "name": "node-bravo",
                    "creationTimestamp": "2026-05-16T12:00:00Z",
                    "labels": {
                        "kubernetes.io/hostname": "node-bravo",
                        "kubernetes.io/os": "linux",
                        "kubernetes.io/arch": "amd64",
                        "node.kubernetes.io/instance-type": "t3.medium",
                        "environment": "database",
                        "topology.kubernetes.io/region": "us-east-1",
                        "topology.kubernetes.io/zone": "us-east-1b"
                    }
                },
                "spec": {
                    "providerID": "aws:///us-east-1b/i-0a1b2c3d4e5f60002"
                },
                "status": {
                    "allocatable": {"memory": "24Gi", "cpu": "4"},
                    "capacity": {"memory": "24Gi", "cpu": "4"},
                    "conditions": [
                        {"type": "Ready", "status": "True"},
                        {"type": "MemoryPressure", "status": "False"},
                        {"type": "DiskPressure", "status": "False"},
                        {"type": "PIDPressure", "status": "False"}
                    ]
                }
            },
            {
                "metadata": {
                    "name": "node-charlie",
                    "creationTimestamp": "2026-05-10T08:30:00Z",
                    "labels": {
                        "kubernetes.io/hostname": "node-charlie",
                        "kubernetes.io/os": "linux",
                        "kubernetes.io/arch": "amd64",
                        "node.kubernetes.io/instance-type": "m5.xlarge",
                        "environment": "analytics",
                        "topology.kubernetes.io/region": "us-east-1",
                        "topology.kubernetes.io/zone": "us-east-1a"
                    }
                },
                "spec": {
                    "providerID": "aws:///us-east-1a/i-0a1b2c3d4e5f60003"
                },
                "status": {
                    "allocatable": {"memory": "32Gi", "cpu": "16"},
                    "capacity": {"memory": "32Gi", "cpu": "16"},
                    "conditions": [
                        {"type": "Ready", "status": "True"},
                        {"type": "MemoryPressure", "status": "True"},
                        {"type": "DiskPressure", "status": "False"},
                        {"type": "PIDPressure", "status": "False"}
                    ]
                }
            },
            {
                "metadata": {
                    "name": "node-delta",
                    "creationTimestamp": joining_time,
                    "labels": {
                        "kubernetes.io/hostname": "node-delta",
                        "kubernetes.io/os": "linux",
                        "kubernetes.io/arch": "amd64",
                        "node.kubernetes.io/instance-type": "t3.medium",
                        "environment": "staging",
                        "topology.kubernetes.io/region": "us-east-1",
                        "topology.kubernetes.io/zone": "us-east-1b"
                    }
                },
                "spec": {
                    "providerID": "aws:///us-east-1b/i-0a1b2c3d4e5f60004",
                    "taints": [{"key": "node.cloudprovider.kubernetes.io/uninitialized", "value": "true", "effect": "NoSchedule"}]
                },
                "status": {
                    "allocatable": {"memory": "16Gi", "cpu": "4"},
                    "capacity": {"memory": "16Gi", "cpu": "4"},
                    "conditions": [
                        {"type": "Ready", "status": "Unknown", "reason": "NodeInitializing", "message": "Kubelet is initializing"},
                        {"type": "MemoryPressure", "status": "False"},
                        {"type": "DiskPressure", "status": "False"},
                        {"type": "PIDPressure", "status": "False"}
                    ]
                }
            },
            {
                "metadata": {
                    "name": "node-echo",
                    "creationTimestamp": "2026-05-22T10:00:00Z",
                    "labels": {
                        "kubernetes.io/hostname": "node-echo",
                        "kubernetes.io/os": "linux",
                        "kubernetes.io/arch": "amd64",
                        "node.kubernetes.io/instance-type": "t3.medium",
                        "environment": "draining",
                        "topology.kubernetes.io/region": "us-east-1",
                        "topology.kubernetes.io/zone": "us-east-1a"
                    }
                },
                "spec": {
                    "providerID": "aws:///us-east-1a/i-0a1b2c3d4e5f60005",
                    "unschedulable": True
                },
                "status": {
                    "allocatable": {"memory": "16Gi", "cpu": "4"},
                    "capacity": {"memory": "16Gi", "cpu": "4"},
                    "conditions": [
                        {"type": "Ready", "status": "Unknown", "reason": "NodeDecommissioning", "message": "Node is being drained and deconstructed"},
                        {"type": "MemoryPressure", "status": "False"},
                        {"type": "DiskPressure", "status": "False"},
                        {"type": "PIDPressure", "status": "False"}
                    ]
                }
            }
        ]
    }
    
    mock_pods = {
        "apiVersion": "v1",
        "kind": "List",
        "metadata": {MOCK_MARKER: True},
        "items": [
            {
                "metadata": {
                    "name": "frontend-pod-7d4f9b8c-2x9v4",
                    "ownerReferences": [{"kind": "ReplicaSet", "name": "frontend-pod-7d4f9b8c", "controller": True}],
                    "labels": {"pod-template-hash": "7d4f9b8c", "app": "frontend"},
                    "namespace": "production",
                    "creationTimestamp": "2026-05-19T06:00:00Z"
                },
                "spec": {"nodeName": "node-alpha", "containers": [{"name": "frontend", "image": "nginx:stable-alpine", "resources": {"requests": {"memory": "12Gi"}}}]},
                "status": {
                    "phase": "Running",
                    "containerStatuses": [{"name": "frontend", "restartCount": 2}]
                }
            },
            {
                "metadata": {
                    "name": "backend-pod-5c8e7a1b-9p3k8",
                    "ownerReferences": [{"kind": "ReplicaSet", "name": "backend-pod-5c8e7a1b", "controller": True}],
                    "labels": {"pod-template-hash": "5c8e7a1b", "app": "backend"},
                    "namespace": "production",
                    "creationTimestamp": "2026-05-20T12:00:00Z"
                },
                "spec": {"nodeName": "node-alpha", "serviceAccountName": "backend",
                         "containers": [{"name": "backend", "image": "node:18-alpine", "resources": {"requests": {"memory": "4Gi"}},
                                         "securityContext": {"runAsNonRoot": True, "runAsUser": 10001},
                                         "env": [{"name": "LOG_LEVEL", "value": "info"},
                                                 {"name": "DB_PASSWORD", "value": "hunter2-demo-only"},
                                                 {"name": "DATABASE_URL", "value": "postgres://app:s3cr3t-demo@db:5432/app"},
                                                 {"name": "STRIPE_API_KEY", "valueFrom": {"secretKeyRef": {"name": "stripe", "key": "api-key"}}}]}]},
                "status": {
                    "phase": "Running",
                    "containerStatuses": [{"name": "backend", "restartCount": 0}]
                }
            },
            {
                "metadata": {
                    "name": "cache-pod-9a3f2c7d-5m1q9",
                    "ownerReferences": [{"kind": "ReplicaSet", "name": "cache-pod-9a3f2c7d", "controller": True}],
                    "labels": {"pod-template-hash": "9a3f2c7d", "app": "cache"},
                    "namespace": "production",
                    "creationTimestamp": "2026-05-21T02:00:00Z"
                },
                "spec": {"nodeName": "node-alpha", "containers": [{"name": "cache", "image": "redis:7-alpine", "resources": {"requests": {"memory": "4Gi"}}}]},
                "status": {
                    "phase": "CrashLoopBackOff",
                    "containerStatuses": [{"name": "cache", "restartCount": 9}]
                }
            },
            {
                "metadata": {
                    "name": "db-pod-0-8b6d4c2e-4w8z7",
                    "labels": {"app": "postgres"},
                    "ownerReferences": [{"kind": "StatefulSet", "name": "db-pod", "controller": True}],
                    "namespace": "database",
                    "creationTimestamp": "2026-05-18T10:00:00Z"
                },
                "spec": {"nodeName": "node-bravo", "containers": [{"name": "db-postgres", "image": "postgres:15-alpine", "resources": {"requests": {"memory": "8Gi"}}}]},
                "status": {
                    "phase": "Running",
                    "containerStatuses": [{"name": "db-postgres", "restartCount": 0}]
                }
            },
            {
                "metadata": {
                    "name": "db-pod-1-8b6d4c2e-5x9a2",
                    "labels": {"app": "postgres"},
                    "ownerReferences": [{"kind": "StatefulSet", "name": "db-pod", "controller": True}],
                    "namespace": "database",
                    "creationTimestamp": "2026-05-18T11:00:00Z"
                },
                "spec": {"nodeName": "node-bravo", "containers": [{"name": "db-postgres", "image": "postgres:15-alpine", "resources": {"requests": {"memory": "8Gi"}}}]},
                "status": {
                    "phase": "Running",
                    "containerStatuses": [{"name": "db-postgres", "restartCount": 0}]
                }
            },
            {
                "metadata": {
                    "name": "stuck-init-pod-5b4c3d2e-1s2a3",
                    "namespace": "production",
                    "creationTimestamp": stuck_time
                },
                "spec": {"nodeName": "node-bravo", "containers": [{"name": "app", "image": "nginx:stable-alpine", "resources": {"requests": {"memory": "2Gi"}}}]},
                "status": {
                    "phase": "Pending",
                    "initContainerStatuses": [{"name": "init-clone", "ready": False, "state": {"waiting": {"reason": "PodInitializing"}}}]
                }
            },
            {
                "metadata": {
                    "name": "normal-init-pod-4w8z7y6x-2x9v4",
                    "namespace": "production",
                    "creationTimestamp": normal_time
                },
                "spec": {"nodeName": "node-bravo", "containers": [{"name": "app", "image": "nginx:stable-alpine", "resources": {"requests": {"memory": "2Gi"}}}]},
                "status": {
                    "phase": "Pending",
                    "initContainerStatuses": [{"name": "init-clone", "ready": False, "state": {"waiting": {"reason": "PodInitializing"}}}]
                }
            },
            {
                "metadata": {
                    "name": "analytics-worker-6f9e8d7c-8y2v4",
                    "namespace": "analytics",
                    "creationTimestamp": "2026-05-20T04:30:00Z"
                },
                "spec": {"nodeName": "node-charlie", "nodeSelector": {"node.kubernetes.io/instance-type": "m5.xlarge"},
                         "containers": [{"name": "worker", "image": "python:3.10-slim", "resources": {"requests": {"memory": "16Gi"}}}]},
                "status": {
                    "phase": "Running",
                    "containerStatuses": [{"name": "worker", "restartCount": 3, "state": {"running": {}}, "ready": True,
                                           "lastState": {"terminated": {"reason": "OOMKilled", "exitCode": 137, "finishedAt": normal_time}}}]
                }
            },
            {
                "metadata": {
                    "name": "logging-agent-3a5b7c9d-1r4q8",
                    "ownerReferences": [{"kind": "DaemonSet", "name": "logging-agent", "controller": True}],
                    "namespace": "kube-system",
                    "creationTimestamp": "2026-05-15T01:00:00Z"
                },
                "spec": {"nodeName": "node-charlie", "hostNetwork": True,
                         "volumes": [{"name": "varlog", "hostPath": {"path": "/var/log"}}, {"name": "sock", "hostPath": {"path": "/var/run/docker.sock"}}],
                         "containers": [{"name": "fluentbit", "image": "fluent/fluent-bit:2-alpine", "resources": {"requests": {"memory": "4Gi"}},
                                         "securityContext": {"privileged": True}}]},
                "status": {
                    "phase": "Running",
                    "containerStatuses": [{"name": "fluentbit", "restartCount": 1}]
                }
            },
            {
                "metadata": {
                    "name": "ml-trainer-7c9d8e6f5-q2w3e",
                    "namespace": "analytics",
                    "creationTimestamp": normal_time
                },
                "spec": {"containers": [{"name": "trainer", "image": "pytorch/pytorch:2.3.0-cuda12.1-cudnn8-runtime", "resources": {"requests": {"memory": "64Gi", "cpu": "16"}}}]},
                "status": {
                    "phase": "Pending",
                    "conditions": [{"type": "PodScheduled", "status": "False", "reason": "Unschedulable", "message": "0/5 nodes are available: 1 node(s) were unschedulable, 4 Insufficient memory. preemption: 0/5 nodes are available: 5 No preemption victims found for incoming pod."}]
                }
            },
            {
                "metadata": {
                    "name": "batch-etl-28734910-x7k2p",
                    "namespace": "analytics",
                    "creationTimestamp": stuck_time,
                    "ownerReferences": [{"kind": "Job", "name": "batch-etl-28734910", "controller": True}]
                },
                "spec": {"containers": [{"name": "etl", "image": "apache/spark:3.5.1", "resources": {"requests": {"memory": "12Gi", "cpu": "6"}}}],
                         "nodeSelector": {"node.kubernetes.io/instance-type": "r6i.4xlarge"}},
                "status": {
                    "phase": "Pending",
                    "conditions": [{"type": "PodScheduled", "status": "False", "reason": "Unschedulable", "lastTransitionTime": stuck_time,
                                    "message": "0/5 nodes are available: 5 node(s) didn't match Pod's node affinity/selector. preemption: 0/5 nodes are available: 5 Preemption is not helpful for scheduling."}]
                }
            },
            {
                "metadata": {
                    "name": "canary-web-6d5f7c8b9-r4t5y",
                    "namespace": "production",
                    "creationTimestamp": normal_time,
                    "ownerReferences": [{"kind": "ReplicaSet", "name": "canary-web-6d5f7c8b9", "controller": True}]
                },
                "spec": {"schedulingGates": [{"name": "example.com/rollout-approval"}],
                         "containers": [{"name": "web", "image": "nginx:stable-alpine", "resources": {"requests": {"memory": "1Gi", "cpu": "500m"}}}]},
                "status": {
                    "phase": "Pending",
                    "conditions": [{"type": "PodScheduled", "status": "False", "reason": "SchedulingGated", "lastTransitionTime": normal_time,
                                    "message": "Scheduling is blocked due to non-empty scheduling gates"}]
                }
            },
            {
                "metadata": {
                    "name": "redis-replica-2",
                    "namespace": "database",
                    "creationTimestamp": joining_time,
                    "ownerReferences": [{"kind": "StatefulSet", "name": "redis-replica", "controller": True}]
                },
                "spec": {"containers": [{"name": "redis", "image": "redis:7-alpine", "resources": {"requests": {"memory": "6Gi", "cpu": "1"}}}]},
                "status": {
                    "phase": "Pending",
                    "conditions": [{"type": "PodScheduled", "status": "False", "reason": "Unschedulable", "lastTransitionTime": joining_time,
                                    "message": "0/5 nodes are available: 1 node(s) had untolerated taint {node.cloudprovider.kubernetes.io/uninitialized: true}, 1 node(s) were unschedulable, 3 Insufficient memory."}]
                }
            },
            {
                "metadata": {
                    "name": "migrate-db-29011744-q8w2e",
                    "namespace": "database",
                    "creationTimestamp": normal_time,
                    "ownerReferences": [{"kind": "Job", "name": "migrate-db-29011744", "controller": True}]
                },
                "spec": {"nodeName": "node-bravo", "containers": [{"name": "migrate", "image": "flyway/flyway:10", "resources": {"requests": {"memory": "512Mi"}}}]},
                "status": {
                    "phase": "Failed",
                    "containerStatuses": [{"name": "migrate", "restartCount": 0, "state": {"terminated": {"reason": "OOMKilled", "exitCode": 137, "finishedAt": normal_time}}}]
                }
            },
            {
                "metadata": {
                    "name": "report-generator-5f6g7h8j9-k1l2m",
                    "namespace": "analytics",
                    "creationTimestamp": stuck_time
                },
                "spec": {"nodeName": "node-charlie", "containers": [{"name": "report", "image": "python:3.10-slim", "resources": {"requests": {"memory": "2Gi"}}}]},
                "status": {
                    "phase": "Failed",
                    "reason": "Evicted",
                    "message": "The node was low on resource: memory. Threshold quantity: 100Mi, available: 85Mi."
                }
            }
        ]
    }
    
    mock_workloads = {
        "apiVersion": "v1", "kind": "List", "metadata": {MOCK_MARKER: True},
        "items": [
            {"kind": "Deployment", "metadata": {"name": "frontend-pod", "namespace": "production"}, "spec": {"replicas": 1},
             "status": {"replicas": 1, "readyReplicas": 1, "availableReplicas": 1,
                        "conditions": [{"type": "Available", "status": "True"}]}},
            {"kind": "Deployment", "metadata": {"name": "backend-pod", "namespace": "production"}, "spec": {"replicas": 1},
             "status": {"replicas": 1, "readyReplicas": 1, "availableReplicas": 1,
                        "conditions": [{"type": "Available", "status": "True"}]}},
            {"kind": "Deployment", "metadata": {"name": "cache-pod", "namespace": "production"}, "spec": {"replicas": 1},
             "status": {"replicas": 1, "readyReplicas": 0, "availableReplicas": 0, "unavailableReplicas": 1,
                        "conditions": [{"type": "Available", "status": "False", "reason": "MinimumReplicasUnavailable",
                                        "message": "Deployment does not have minimum availability.", "lastTransitionTime": stuck_time}]}},
            {"kind": "StatefulSet", "metadata": {"name": "db-pod", "namespace": "database"}, "spec": {"replicas": 2},
             "status": {"replicas": 2, "readyReplicas": 2, "currentReplicas": 2}},
            {"kind": "PodDisruptionBudget", "metadata": {"name": "frontend-pdb", "namespace": "production"},
             "spec": {"minAvailable": 1, "selector": {"matchLabels": {"app": "frontend"}}},
             "status": {"currentHealthy": 1, "desiredHealthy": 1, "disruptionsAllowed": 0, "expectedPods": 1}},
            {"kind": "PodDisruptionBudget", "metadata": {"name": "postgres-pdb", "namespace": "database"},
             "spec": {"maxUnavailable": 1, "selector": {"matchLabels": {"app": "postgres"}}},
             "status": {"currentHealthy": 2, "desiredHealthy": 1, "disruptionsAllowed": 1, "expectedPods": 2}},
            {"kind": "StatefulSet", "metadata": {"name": "redis-replica", "namespace": "database"}, "spec": {"replicas": 3},
             "status": {"replicas": 3, "readyReplicas": 2, "currentReplicas": 3}},
            {"kind": "DaemonSet", "metadata": {"name": "logging-agent", "namespace": "kube-system"},
             "status": {"desiredNumberScheduled": 4, "currentNumberScheduled": 1, "numberReady": 1, "numberAvailable": 1}}
        ]
    }
    workloads_file = get_workloads_path(context)
    if not os.path.exists(workloads_file) or is_generated_mock(workloads_file):
        write_file_atomic(workloads_file, json.dumps(redaction.sanitize_list_for_disk(mock_workloads), indent=2))

    if not os.path.exists(nodes_file) or is_generated_mock(nodes_file):
        write_file_atomic(nodes_file, json.dumps(redaction.sanitize_list_for_disk(mock_nodes), indent=2))
        print(f"Generated standard mock {nodes_file}.")

    if not os.path.exists(pods_file) or is_generated_mock(pods_file):
        write_file_atomic(pods_file, json.dumps(redaction.sanitize_list_for_disk(mock_pods), indent=2))
        print(f"Generated standard mock {pods_file}.")

def is_restartable_init_container(container_spec):
    """Native sidecars (Kubernetes 1.29+) are init containers with restartPolicy: Always."""
    return (container_spec or {}).get("restartPolicy") == "Always"

def get_detailed_pod_status(pod):
    """
    Computes the pod status string shown in the STATUS column of `kubectl get pods`
    (port of kubectl's printPod logic: Init:N/M, CrashLoopBackOff, Completed, Evicted, Terminating, ...).
    """
    status = pod.get("status") or {}
    spec = pod.get("spec") or {}
    metadata = pod.get("metadata") or {}

    reason = status.get("reason") or status.get("phase") or "Unknown"

    # 1. Init containers (regular ones must finish; native sidecars only need to have started)
    init_specs = {c.get("name"): c for c in spec.get("initContainers") or []}
    init_statuses = status.get("initContainerStatuses") or []
    init_total = len(init_specs) or len(init_statuses)
    initializing = False
    for i, cs in enumerate(init_statuses):
        state = cs.get("state") or {}
        terminated = state.get("terminated")
        waiting = state.get("waiting")
        if terminated and terminated.get("exitCode", 0) == 0:
            continue
        if is_restartable_init_container(init_specs.get(cs.get("name"))) and cs.get("started"):
            continue
        initializing = True
        if terminated:
            if terminated.get("reason"):
                reason = f"Init:{terminated['reason']}"
            elif terminated.get("signal"):
                reason = f"Init:Signal:{terminated['signal']}"
            else:
                reason = f"Init:ExitCode:{terminated.get('exitCode', 0)}"
        elif waiting and waiting.get("reason") and waiting.get("reason") != "PodInitializing":
            reason = f"Init:{waiting['reason']}"
        else:
            reason = f"Init:{i}/{init_total}"
        break

    # 2. App containers (kubectl walks them in reverse, so the first container's state wins)
    if not initializing:
        has_running = False
        for cs in reversed(status.get("containerStatuses") or []):
            state = cs.get("state") or {}
            waiting = state.get("waiting")
            terminated = state.get("terminated")
            if waiting and waiting.get("reason"):
                reason = waiting["reason"]
            elif terminated and terminated.get("reason"):
                reason = terminated["reason"]
            elif terminated:
                if terminated.get("signal"):
                    reason = f"Signal:{terminated['signal']}"
                else:
                    reason = f"ExitCode:{terminated.get('exitCode', 0)}"
            elif "running" in state and cs.get("ready"):
                has_running = True

        # A finished sidecar shouldn't make a running pod look "Completed"
        if reason == "Completed" and has_running:
            pod_ready = any(c.get("type") == "Ready" and c.get("status") == "True" for c in status.get("conditions") or [])
            reason = "Running" if pod_ready else "NotReady"

    # 3. Deletion in progress
    if metadata.get("deletionTimestamp"):
        reason = "Unknown" if status.get("reason") == "NodeLost" else "Terminating"

    return reason

def container_request(container, resource):
    """A container's request for a resource, falling back to its limit (Kubernetes defaults request to limit)."""
    resources = container.get("resources") or {}
    value = (resources.get("requests") or {}).get(resource) or (resources.get("limits") or {}).get(resource)
    return parse_quantity(value)

def effective_pod_request(spec, resource):
    """
    The amount of a resource the scheduler reserves for a pod, in base units:
    max(sum(app containers) + sum(sidecars), peak during init) + pod overhead.
    """
    app_sum = sum(container_request(c, resource) for c in spec.get("containers") or [])
    sidecar_sum = 0.0
    init_peak = 0.0
    for c in spec.get("initContainers") or []:
        req = container_request(c, resource)
        if is_restartable_init_container(c):
            sidecar_sum += req
            init_peak = max(init_peak, sidecar_sum)
        else:
            init_peak = max(init_peak, sidecar_sum + req)
    total = max(app_sum + sidecar_sum, init_peak)
    return total + parse_quantity((spec.get("overhead") or {}).get(resource))

def get_pod_condition(pod, condition_type):
    for c in (pod.get("status") or {}).get("conditions") or []:
        if c.get("type") == condition_type:
            return c
    return None

def parse_timestamp_to_datetime(ts_str):
    if not ts_str:
        return None
    try:
        return datetime.datetime.fromisoformat(ts_str.replace('Z', '+00:00'))
    except Exception:
        try:
            return datetime.datetime.strptime(ts_str, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)
        except Exception:
            return None

AWS_LABEL_PREFIXES = ("eks.amazonaws.com/", "karpenter.k8s.aws/", "alpha.eksctl.io/", "k8s.amazonaws.com/")

def check_is_aws_node(node):
    """Detects AWS nodes from spec.providerID (authoritative), falling back to AWS-specific label keys."""
    provider_id = (node.get("spec") or {}).get("providerID") or ""
    if provider_id:
        return provider_id.startswith("aws://")
    labels = (node.get("metadata") or {}).get("labels") or {}
    return any(k.startswith(AWS_LABEL_PREFIXES) for k in labels)

def get_capacity_type(labels):
    """Normalizes capacity-type labels (Karpenter, EKS managed node groups) to 'spot' / 'on-demand'."""
    raw = (labels.get("karpenter.sh/capacity-type")
           or labels.get("eks.amazonaws.com/capacityType")
           or labels.get("node.kubernetes.io/lifecycle"))
    if not raw:
        return "on-demand"
    value = str(raw).lower().replace("_", "-")
    if value == "spot":
        return "spot"
    if value in ("on-demand", "normal"):
        return "on-demand"
    return value

COST_DATA_DIR = os.path.join(APP_DIR, "supplements", "cost_data", "aws")
_aws_costs_cache = {}

def load_aws_costs(csv_path):
    costs = {}
    if not os.path.exists(csv_path):
        print(f"▲ Warning: Cost CSV file not found at {csv_path}")
        return costs

    import csv
    try:
        with open(csv_path, mode='r', encoding='utf-8') as f:
            reader = csv.reader(f)
            headers = next(reader)
            api_name_idx = -1
            on_demand_idx = -1
            for i, h in enumerate(headers):
                if h.strip() == "API Name":
                    api_name_idx = i
                elif h.strip() == "On Demand":
                    on_demand_idx = i

            if api_name_idx == -1 or on_demand_idx == -1:
                api_name_idx = 1
                on_demand_idx = 9

            for row in reader:
                if len(row) > max(api_name_idx, on_demand_idx):
                    api_name = row[api_name_idx].strip()
                    on_demand_str = row[on_demand_idx].strip()
                    cost_match = re.match(r'^\$([0-9.]+)\s*hourly$', on_demand_str)
                    if cost_match:
                        try:
                            costs[api_name] = float(cost_match.group(1))
                        except ValueError:
                            pass
    except Exception as e:
        print(f"▲ Error reading CSV {csv_path}: {e}")
    return costs

def get_region_costs(region):
    """On-demand hourly prices for a region (e.g. 'us-west-2'), loaded once and cached. None if no data."""
    if not region or not re.match(r'^[a-z]{2}-[a-z]+-[0-9]+$', region):
        return None
    if region not in _aws_costs_cache:
        csv_path = os.path.join(COST_DATA_DIR, f"aws_ec2_{region.replace('-', '_')}.csv")
        _aws_costs_cache[region] = load_aws_costs(csv_path) if os.path.exists(csv_path) else None
    return _aws_costs_cache[region]

def get_node_cost_details(node):
    """
    Static pricing facts for a node. Elapsed/lifetime cost is derived on the client from
    hourlyCost + creationTimestamp so this payload doesn't change on every scrape.
    """
    if not check_is_aws_node(node):
        return None
    labels = (node.get("metadata") or {}).get("labels") or {}
    if labels.get("eks.amazonaws.com/compute-type") == "fargate":
        return None
    instance_type = labels.get("node.kubernetes.io/instance-type") or labels.get("beta.kubernetes.io/instance-type")
    region = labels.get("topology.kubernetes.io/region") or labels.get("failure-domain.beta.kubernetes.io/region")
    if not instance_type or not region:
        return None
    region_costs = get_region_costs(region)
    if not region_costs:
        return None
    hourly_cost = region_costs.get(instance_type)
    if hourly_cost is None:
        return None
    capacity_type = get_capacity_type(labels)
    return {
        "provider": "aws",
        "region": region,
        "instanceType": instance_type,
        "capacityType": capacity_type,
        "hourlyCost": hourly_cost,
        # We only ship on-demand list prices; spot runs are usually far cheaper, so flag them as estimates
        "pricingBasis": "on-demand-list",
        "isEstimate": capacity_type != "on-demand",
    }

def summarize_containers(spec):
    """Name / image / requests per container: what the hover panel shows, without the full pod spec."""
    out = []
    for kind, containers in (("init", spec.get("initContainers") or []), ("app", spec.get("containers") or [])):
        for c in containers:
            requests = (c.get("resources") or {}).get("requests") or {}
            item = {"name": c.get("name"), "image": c.get("image"), "cpu": requests.get("cpu"), "memory": requests.get("memory")}
            if kind == "init":
                item["init"] = True
                if is_restartable_init_container(c):
                    item["sidecar"] = True
            out.append(item)
    return out

def get_owner(metadata):
    """The controlling owner (e.g. ReplicaSet/StatefulSet/DaemonSet/Job), if any."""
    refs = metadata.get("ownerReferences") or []
    ref = next((r for r in refs if r.get("controller")), refs[0] if refs else None)
    return {"kind": ref.get("kind"), "name": ref.get("name")} if ref else None

def resolve_workload(metadata):
    """The top-level workload owning a pod (a ReplicaSet owner maps to its Deployment)."""
    owner = get_owner(metadata)
    if not owner:
        return None
    if owner["kind"] == "ReplicaSet":
        rs_name = owner["name"] or ""
        template_hash = ((metadata.get("labels") or {}).get("pod-template-hash")) or ""
        if template_hash and rs_name.endswith("-" + template_hash):
            return {"kind": "Deployment", "name": rs_name[: -(len(template_hash) + 1)]}
        return {"kind": "Deployment", "name": rs_name.rsplit("-", 1)[0]} if "-" in rs_name else owner
    return owner

def summarize_workloads(workloads_data):
    """Desired vs ready replicas for Deployments, StatefulSets and DaemonSets."""
    out = []
    for item in (workloads_data or {}).get("items", []):
        kind = item.get("kind")
        md, spec, status = item.get("metadata") or {}, item.get("spec") or {}, item.get("status") or {}
        if kind == "DaemonSet":
            desired = status.get("desiredNumberScheduled") or 0
            ready = status.get("numberReady") or 0
            available = status.get("numberAvailable") or 0
        elif kind in ("Deployment", "StatefulSet"):
            desired = spec.get("replicas", 1) or 0
            ready = status.get("readyReplicas") or 0
            available = status.get("availableReplicas", ready) or 0
        else:
            continue
        conds = {c.get("type"): c for c in status.get("conditions") or []}
        avail_cond = conds.get("Available") or {}
        progressing = conds.get("Progressing") or {}
        out.append({
            "kind": kind,
            "namespace": md.get("namespace", "default"),
            "name": md.get("name"),
            "desired": desired,
            "ready": ready,
            "available": available,
            "unavailableSince": avail_cond.get("lastTransitionTime") if avail_cond.get("status") == "False" else None,
            "stalled": progressing.get("reason") == "ProgressDeadlineExceeded",
            "message": progressing.get("message") if progressing.get("reason") == "ProgressDeadlineExceeded" else avail_cond.get("message"),
        })
    out.sort(key=lambda w: (w["namespace"], w["kind"], w["name"]))
    return out

def summarize_pdbs(workloads_data):
    """PodDisruptionBudgets: selector + how many voluntary evictions they currently allow."""
    out = []
    for item in (workloads_data or {}).get("items", []):
        if item.get("kind") != "PodDisruptionBudget":
            continue
        md, spec, status = item.get("metadata") or {}, item.get("spec") or {}, item.get("status") or {}
        selector = spec.get("selector") or {}
        out.append({
            "namespace": md.get("namespace", "default"),
            "name": md.get("name"),
            "matchLabels": selector.get("matchLabels") or {},
            "hasExpressions": bool(selector.get("matchExpressions")),
            "minAvailable": spec.get("minAvailable"),
            "maxUnavailable": spec.get("maxUnavailable"),
            "disruptionsAllowed": status.get("disruptionsAllowed"),
            "currentHealthy": status.get("currentHealthy"),
            "desiredHealthy": status.get("desiredHealthy"),
            "expectedPods": status.get("expectedPods"),
        })
    out.sort(key=lambda p: (p["namespace"], p["name"]))
    return out

def summarize_scheduling(spec):
    """What the scheduler needs beyond requests: nodeSelector and tolerations (affinity is not modeled)."""
    tolerations = [{k: t.get(k) for k in ("key", "operator", "value", "effect") if t.get(k) is not None}
                   for t in spec.get("tolerations") or []]
    out = {}
    if spec.get("nodeSelector"):
        out["nodeSelector"] = spec["nodeSelector"]
    if tolerations:
        out["tolerations"] = tolerations
    if (spec.get("affinity") or {}).get("nodeAffinity"):
        out["hasNodeAffinity"] = True
    return out

def fingerprint(obj):
    """Short stable content hash, used by the frontend to skip rebuilding unchanged nodes."""
    return hashlib.sha1(json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()[:16]

def get_last_termination(status):
    """The most recent container termination (current or previous state), e.g. to spot OOMKilled."""
    latest = None
    for cs in (status.get("initContainerStatuses") or []) + (status.get("containerStatuses") or []):
        for terminated in ((cs.get("state") or {}).get("terminated"), (cs.get("lastState") or {}).get("terminated")):
            if not terminated:
                continue
            finished = terminated.get("finishedAt") or ""
            if latest is None or finished > (latest.get("finishedAt") or ""):
                latest = {"container": cs.get("name"), "reason": terminated.get("reason"),
                          "exitCode": terminated.get("exitCode"), "finishedAt": terminated.get("finishedAt")}
    return latest

def summarize_pod_resources(spec):
    mem_gb = effective_pod_request(spec, "memory") / (1024**3)
    cpu_cores = effective_pod_request(spec, "cpu")
    return mem_gb, cpu_cores

def parse_cluster(context=None, force_mock=False, write_to_file=True, custom_output_file=None):
    nodes_file, pods_file, output_file = get_file_paths(context)
    if custom_output_file:
        output_file = custom_output_file
    ensure_data_dir()

    # 1. Gather data (live cluster pull or force mock)
    if force_mock:
        create_mock_files_if_missing(context)
    else:
        live_success = run_kubectl(context)
        if not live_success:
            if context:
                print(f"Error: Failed to query cluster context '{context}'")
                sys.exit(1)
            print("▲ Live cluster query skipped/failed. Processing via local files...")
            create_mock_files_if_missing(context)

    # 2. Read nodes
    if not os.path.exists(nodes_file):
        print(f"Error: {nodes_file} is missing. Cannot parse.")
        sys.exit(1)

    with open(nodes_file, "r", encoding="utf-8") as f:
        try:
            nodes_data = json.load(f)
        except json.JSONDecodeError as e:
            print(f"Error parsing {nodes_file}: {e}")
            sys.exit(1)

    # 3. Read pods
    if not os.path.exists(pods_file):
        print(f"Error: {pods_file} is missing. Cannot parse.")
        sys.exit(1)

    with open(pods_file, "r", encoding="utf-8") as f:
        try:
            pods_data = json.load(f)
        except json.JSONDecodeError as e:
            print(f"Error parsing {pods_file}: {e}")
            sys.exit(1)

    # Dictionary to structure node maps
    node_map = {}

    # Gather nodes
    node_list = nodes_data.get("items", [])
    if not node_list:
        print("Warning: No nodes found in the nodes json file.")

    for node in node_list:
        metadata = node.get("metadata", {})
        name = metadata.get("name")
        if not name:
            continue

        status = node.get("status", {})
        allocatable = status.get("allocatable", {})
        capacity = status.get("capacity", {})

        # Get allocatable memory string, fall back to capacity
        mem_str = allocatable.get("memory") or capacity.get("memory", "8Gi")
        max_mem_gb = parse_memory_to_gb(mem_str)

        # Get allocatable cpu string, fall back to capacity
        cpu_str = allocatable.get("cpu") or capacity.get("cpu", "1")
        max_cpu_cores = parse_cpu_to_cores(cpu_str)

        # Extract labels and conditions
        labels = metadata.get("labels", {})
        conditions_raw = status.get("conditions", [])
        conditions = {}
        for cond in conditions_raw:
            c_type = cond.get("type")
            c_status = cond.get("status")
            if c_type and c_status:
                conditions[c_type] = c_status
                # Record reason and message to allow detailed frontend state logic
                c_reason = cond.get("reason")
                c_message = cond.get("message")
                if c_reason:
                    conditions[c_type + "Reason"] = c_reason
                if c_message:
                    conditions[c_type + "Message"] = c_message
                # When the condition last changed state (used for "not ready since" / "pressure since")
                if cond.get("lastTransitionTime"):
                    conditions[c_type + "Since"] = cond["lastTransitionTime"]

        spec = node.get("spec", {})
        unschedulable = spec.get("unschedulable", False)
        taints = [{"key": t.get("key"), "value": t.get("value"), "effect": t.get("effect")} for t in spec.get("taints") or [] if t.get("key")]

        creation_ts = metadata.get("creationTimestamp")

        node_map[name] = {
            "name": name,
            "maxMemoryGB": round_precise(max_mem_gb),
            "maxCPUCores": round_precise(max_cpu_cores),
            "labels": labels,
            "conditions": conditions,
            "taints": taints,
            "creationTimestamp": creation_ts,
            "costDetails": get_node_cost_details(node),
            "unschedulable": unschedulable,
            "providerID": spec.get("providerID"),
            "pods": []
        }

    # Process pods
    unscheduled_pods = []
    failed_pods = []
    pod_list = pods_data.get("items", [])
    for pod in pod_list:
        metadata = pod.get("metadata", {})
        name = metadata.get("name")
        namespace = metadata.get("namespace", "default")

        spec = pod.get("spec", {})
        node_name = spec.get("nodeName")

        status = pod.get("status", {})
        phase = status.get("phase", "Unknown")
        detailed_status = get_detailed_pod_status(pod)

        total_pod_memory_gb, total_pod_cpu_cores = summarize_pod_resources(spec)

        # Succeeded/Failed pods release their node resources; keep a summary of failures (e.g. Evicted)
        if phase in ("Succeeded", "Failed"):
            if phase == "Failed":
                failed_pods.append({
                    "name": name,
                    "namespace": namespace,
                    "uid": metadata.get("uid"),
                    "nodeName": node_name,
                    "status": detailed_status,
                    "reason": status.get("reason"),
                    "message": status.get("message"),
                    "memoryGB": round_precise(total_pod_memory_gb),
                    "cpuCores": round_precise(total_pod_cpu_cores),
                    "color": get_namespace_color(namespace),
                    "owner": get_owner(metadata),
                    "lastTermination": get_last_termination(status),
                    "creationTimestamp": metadata.get("creationTimestamp"),
                })
            continue

        # Pods not yet bound to a node: record why the scheduler can't place them
        if not node_name:
            sched = get_pod_condition(pod, "PodScheduled") or {}
            unscheduled_pods.append({
                "name": name,
                "namespace": namespace,
                "uid": metadata.get("uid"),
                "status": detailed_status,
                "reason": sched.get("reason"),
                "message": sched.get("message"),
                # When the scheduler last evaluated the pod (falls back to creation time)
                "since": sched.get("lastTransitionTime") or metadata.get("creationTimestamp"),
                "memoryGB": round_precise(total_pod_memory_gb),
                "cpuCores": round_precise(total_pod_cpu_cores),
                "color": get_namespace_color(namespace),
                "owner": get_owner(metadata),
                "priorityClassName": spec.get("priorityClassName"),
                "workload": resolve_workload(metadata),
                "creationTimestamp": metadata.get("creationTimestamp"),
            })
            continue

        # Fallback if no container memory requests/limits are specified.
        # Set to 0.1 GB (100Mi) to ensure pod still renders as a sleek 3D room.
        if total_pod_memory_gb <= 0.0:
            total_pod_memory_gb = 0.10

        if total_pod_cpu_cores <= 0.0:
            total_pod_cpu_cores = 0.10

        total_pod_memory_gb = round_precise(total_pod_memory_gb)
        total_pod_cpu_cores = round_precise(total_pod_cpu_cores)

        # Stable hash-based color matching for the pod namespace
        color_int = get_namespace_color(namespace)

        # Sum container and init container restarts
        restarts = 0
        container_statuses = status.get("containerStatuses", [])
        for cs in container_statuses:
            restarts += cs.get("restartCount", 0)
        init_container_statuses = status.get("initContainerStatuses", [])
        for ics in init_container_statuses:
            restarts += ics.get("restartCount", 0)

        pod_item = {
            "name": name,
            "memoryGB": total_pod_memory_gb,
            "cpuCores": total_pod_cpu_cores,
            "color": color_int,
            "status": detailed_status,
            "namespace": namespace,
            "restarts": restarts,
            "creationTimestamp": metadata.get("creationTimestamp"),
            "uid": metadata.get("uid"),
            "workload": resolve_workload(metadata),
            "phase": phase,
            "labels": metadata.get("labels") or {},
            "owner": get_owner(metadata),
            "podIP": status.get("podIP"),
            "hostIP": status.get("hostIP"),
            "containers": summarize_containers(spec),
            "lastTermination": get_last_termination(status),
            "security": security.summarize(pod, namespace, resolve_workload(metadata)),
            "scheduling": summarize_scheduling(spec)
        }

        # If a pod is scheduled to a node not in our nodes inventory, create a placeholder node
        if node_name not in node_map:
            # Estimate a reasonable standard capacity, e.g., 16 GB, or at least enough for the pod
            placeholder_capacity = max(16.0, total_pod_memory_gb)
            placeholder_cpu = max(4.0, total_pod_cpu_cores)
            node_map[node_name] = {
                "name": node_name,
                "maxMemoryGB": placeholder_capacity,
                "maxCPUCores": placeholder_cpu,
                "labels": {
                    "kubernetes.io/hostname": node_name,
                    "kubernetes.io/os": "linux",
                    "note": "auto-created-placeholder"
                },
                "conditions": {
                    "Ready": "True",
                    "MemoryPressure": "False",
                    "DiskPressure": "False",
                    "PIDPressure": "False"
                },
                "taints": [],
                "creationTimestamp": None,
                "costDetails": None,
                "unschedulable": False,
                "pods": []
            }

        node_map[node_name]["pods"].append(pod_item)

    # Format the completed cluster configuration
    compiled_nodes = list(node_map.values())

    # Sort nodes alphabetically for structured rendering layout
    compiled_nodes.sort(key=lambda x: x["name"])
    for n in compiled_nodes:
        n["fingerprint"] = fingerprint(n)

    if force_mock and (not context or context == "demo"):
        cluster_name = "demo"  # don't label the demo fixture with whatever kubectl context happens to be active
    else:
        cluster_name = get_cluster_name(context) or "ClusterNamePlaceHolder"

    unscheduled_pods.sort(key=lambda p: (-(p["memoryGB"] or 0), p["namespace"], p["name"]))
    workloads, pdbs = [], []
    workloads_file = get_workloads_path(context)
    if os.path.exists(workloads_file):
        try:
            with open(workloads_file, "r", encoding="utf-8") as f:
                workloads_data = json.load(f)
            workloads = summarize_workloads(workloads_data)
            pdbs = summarize_pdbs(workloads_data)
        except (OSError, ValueError) as e:
            print(f"▲ Could not read {workloads_file}: {e}")
    failed_pods.sort(key=lambda p: p.get("creationTimestamp") or "", reverse=True)

    output_state = {
        "clusterName": cluster_name,
        "nodes": compiled_nodes,
        "unscheduledPods": unscheduled_pods,
        "failedPods": failed_pods,
        "workloads": workloads,
        "pdbs": pdbs
    }
    # Actionable alerts (stable `since` timestamps, so they don't change the hash of an unchanged cluster)
    output_state["alerts"] = alerts.evaluate(output_state, context=context or ("demo" if force_mock else None))

    # Hash of everything except the timestamp: unchanged clusters keep the same ETag across scrapes
    output_state["contentHash"] = fingerprint(output_state)
    output_state["generatedAt"] = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Write out Compiled cluster_state.json if requested
    if write_to_file:
        write_file_atomic(output_file, json.dumps(output_state, separators=(",", ":")))
        print(f"✔ Compiled cluster environment state written to '{output_file}' for cluster: {cluster_name}.")

    print(f"✔ Successfully parsed {len(node_list)} nodes and {len(pod_list)} pods "
          f"({len(unscheduled_pods)} unscheduled, {len(failed_pods)} failed).")
    return output_state

if __name__ == "__main__":
    context = None
    if len(sys.argv) > 2 and sys.argv[1] == "--context":
        context = sys.argv[2]
    elif len(sys.argv) > 1 and sys.argv[1].startswith("--context="):
        context = sys.argv[1].split("=", 1)[1]
        
    if context and not re.match(r'^[a-zA-Z0-9_.:@/][a-zA-Z0-9_./:@-]*$', context):
        print(f"Error: Invalid context name format '{context}'", file=sys.stderr)
        sys.exit(1)
        
    parse_cluster(context)
