#!/usr/bin/env python3
import os
import sys
import json
import re
import subprocess
import hashlib
import datetime

# Configuration
NODES_RAW_FILE = "raw-nodes.json"
PODS_RAW_FILE = "raw-pods.json"
OUTPUT_FILE = "cluster_state.json"

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
]

def get_namespace_color(namespace):
    """Generates a stable color integer from a namespace string using MD5 hashing."""
    h = hashlib.md5(namespace.encode('utf-8')).hexdigest()
    idx = int(h, 16) % len(NAMESPACE_COLORS)
    return NAMESPACE_COLORS[idx]

def round_to_one_decimal(val):
    """Rounds a float to the nearest 1 decimal place, returning an integer if it's a whole number."""
    rounded = round(val, 1)
    if rounded == int(rounded):
        return int(rounded)
    return rounded

def parse_cpu_to_cores(cpu_str):
    """
    Parses messy Kubernetes CPU resource strings to floating-point cores.
    Handles millicores (e.g. 500m, 100m) and raw core numbers.
    """
    if not cpu_str:
        return 0.0
    
    cpu_str = str(cpu_str).strip()
    
    # Match values like 500m, 2, 1.5, 0.25
    match = re.match(r'^([0-9]+(?:\.[0-9]+)?)\s*([a-zA-Z]*)$', cpu_str)
    if not match:
        return 0.0
        
    val_str, unit = match.groups()
    val = float(val_str)
    unit = unit.lower()
    
    if unit == 'm':
        cores = val / 1000.0
    else:
        # Default to raw cores
        cores = val
        
    return round_to_one_decimal(cores)

def parse_memory_to_gb(mem_str):
    """
    Parses messy Kubernetes resource strings to floating-point Gigabytes.
    Handles binary exponents (Ki, Mi, Gi, Ti, Pi, Ei),
    decimal exponents (k, M, G, T, P, E), and raw byte integers.
    """
    if not mem_str:
        return 0.0
    
    mem_str = str(mem_str).strip()
    
    # Match values like 24Gi, 100Mi, 32551416Ki, 16G, 8000000
    match = re.match(r'^([0-9]+(?:\.[0-9]+)?)\s*([a-zA-Z]*)$', mem_str)
    if not match:
        return 0.0
    
    val_str, unit = match.groups()
    val = float(val_str)
    unit = unit.lower()
    
    # Binary power multipliers (1024 base)
    binary_units = {
        'ki': 1024,
        'mi': 1024**2,
        'gi': 1024**3,
        'ti': 1024**4,
        'pi': 1024**5,
        'ei': 1024**6
    }
    
    # Decimal power multipliers (1000 base)
    decimal_units = {
        'k': 1000,
        'm': 1000**2,
        'g': 1000**3,
        't': 1000**4,
        'p': 1000**5,
        'e': 1000**6
    }
    
    if unit in binary_units:
        bytes_val = val * binary_units[unit]
    elif unit in decimal_units:
        bytes_val = val * decimal_units[unit]
    else:
        # Default to raw bytes
        bytes_val = val
        
    # Convert bytes to Gigabytes (binary GiB = 1024^3 bytes)
    gb_val = bytes_val / (1024**3)
    return round_to_one_decimal(gb_val)

def run_kubectl(context=None):
    """Attempts to run kubectl to fetch live cluster JSONs."""
    if context:
        print(f"Attempting to query Kubernetes cluster context: {context}...")
    else:
        print("Attempting to query active Kubernetes cluster context...")
        
    try:
        # Check if kubectl command exists
        subprocess.run(["kubectl", "version", "--client"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        print("▲ Note: 'kubectl' CLI utility is not installed or not in system PATH.")
        return False

    base_cmd = ["kubectl"]
    if context:
        base_cmd += ["--context", context]

    try:
        # Check active cluster connection
        if context:
            # Check cluster connection with a 2-second timeout
            subprocess.run(base_cmd + ["cluster-info", "--request-timeout=2s"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        else:
            subprocess.run(["kubectl", "config", "current-context"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
    except subprocess.CalledProcessError:
        if context:
            print(f"▲ Note: Failed to connect to context '{context}'.")
        else:
            print("▲ Note: 'kubectl' is installed, but no active cluster context was detected.")
        return False

    # Fetch nodes
    try:
        print(f"Fetching nodes list and writing to {NODES_RAW_FILE}...")
        nodes_res = subprocess.run(base_cmd + ["get", "nodes", "-o", "json"], capture_output=True, text=True, check=True)
        with open(NODES_RAW_FILE, "w", encoding="utf-8") as f:
            f.write(nodes_res.stdout)
    except subprocess.CalledProcessError as e:
        print(f"▲ Error fetching nodes: {e.stderr}")
        return False

    # Fetch pods
    try:
        print(f"Fetching pods list across all namespaces and writing to {PODS_RAW_FILE}...")
        pods_res = subprocess.run(base_cmd + ["get", "pods", "--all-namespaces", "-o", "json"], capture_output=True, text=True, check=True)
        with open(PODS_RAW_FILE, "w", encoding="utf-8") as f:
            f.write(pods_res.stdout)
    except subprocess.CalledProcessError as e:
        print(f"▲ Error fetching pods: {e.stderr}")
        return False

    print("● Live cluster data successfully extracted!")
    return True

def get_cluster_name(context=None):
    """Attempts to fetch active Kubernetes context/cluster name."""
    if context:
        return context
    try:
        res = subprocess.run(["kubectl", "config", "current-context"], capture_output=True, text=True, check=True)
        return res.stdout.strip()
    except Exception:
        return None

def create_mock_files_if_missing():
    """Generates standard mock JSON files if they don't exist in the directory."""
    if os.path.exists(NODES_RAW_FILE) and os.path.exists(PODS_RAW_FILE):
        return

    print("▲ Local raw Kubernetes fixtures not found. Generating default mock files...")
    
    # Generate dynamic creation timestamps for stuck and normal init pods relative to current run time
    now = datetime.datetime.now(datetime.timezone.utc)
    stuck_time = (now - datetime.timedelta(minutes=45)).strftime("%Y-%m-%dT%H:%M:%SZ")
    normal_time = (now - datetime.timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    
    mock_nodes = {
        "apiVersion": "v1",
        "kind": "List",
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
                        "topology.kubernetes.io/region": "us-east-1",
                        "topology.kubernetes.io/zone": "us-east-1a"
                    }
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
                "status": {
                    "allocatable": {"memory": "16Gi", "cpu": "4"},
                    "capacity": {"memory": "16Gi", "cpu": "4"},
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
                    "creationTimestamp": "2026-05-21T09:00:00Z",
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
            }
        ]
    }
    
    mock_pods = {
        "apiVersion": "v1",
        "kind": "List",
        "items": [
            {
                "metadata": {
                    "name": "frontend-pod-7d4f9b8c-2x9v4",
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
                    "namespace": "production",
                    "creationTimestamp": "2026-05-20T12:00:00Z"
                },
                "spec": {"nodeName": "node-alpha", "containers": [{"name": "backend", "image": "node:18-alpine", "resources": {"requests": {"memory": "4Gi"}}}]},
                "status": {
                    "phase": "Running",
                    "containerStatuses": [{"name": "backend", "restartCount": 0}]
                }
            },
            {
                "metadata": {
                    "name": "cache-pod-9a3f2c7d-5m1q9",
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
                "spec": {"nodeName": "node-charlie", "containers": [{"name": "worker", "image": "python:3.10-slim", "resources": {"requests": {"memory": "16Gi"}}}]},
                "status": {
                    "phase": "Running",
                    "containerStatuses": [{"name": "worker", "restartCount": 3}]
                }
            },
            {
                "metadata": {
                    "name": "logging-agent-3a5b7c9d-1r4q8",
                    "namespace": "kube-system",
                    "creationTimestamp": "2026-05-15T01:00:00Z"
                },
                "spec": {"nodeName": "node-charlie", "containers": [{"name": "fluentbit", "image": "fluent/fluent-bit:2-alpine", "resources": {"requests": {"memory": "4Gi"}}}]},
                "status": {
                    "phase": "Running",
                    "containerStatuses": [{"name": "fluentbit", "restartCount": 1}]
                }
            }
        ]
    }
    
    if not os.path.exists(NODES_RAW_FILE):
        with open(NODES_RAW_FILE, "w", encoding="utf-8") as f:
            json.dump(mock_nodes, f, indent=2)
        print(f"Generated standard mock {NODES_RAW_FILE}.")
        
    if not os.path.exists(PODS_RAW_FILE):
        with open(PODS_RAW_FILE, "w", encoding="utf-8") as f:
            json.dump(mock_pods, f, indent=2)
        print(f"Generated standard mock {PODS_RAW_FILE}.")

def get_detailed_pod_status(pod):
    """
    Computes a high-fidelity, user-friendly detailed status string for a pod,
    matching kubectl's logic (e.g. ContainerCreating, Init:0/1, CrashLoopBackOff, Running).
    """
    status = pod.get("status", {})
    phase = status.get("phase", "Unknown")
    
    # 1. Check if pod deletion is in progress
    metadata = pod.get("metadata", {})
    if metadata.get("deletionTimestamp"):
        return "Terminating"
        
    # 2. Check Init Containers status
    init_statuses = status.get("initContainerStatuses", [])
    for i, cs in enumerate(init_statuses):
        state = cs.get("state", {})
        waiting = state.get("waiting", {})
        terminated = state.get("terminated", {})
        
        if waiting:
            reason = waiting.get("reason", "")
            if reason == "PodInitializing":
                return f"Init:{i}/{len(init_statuses)}"
            return f"Init:{reason or 'Waiting'}"
        elif terminated:
            exit_code = terminated.get("exitCode", 0)
            if exit_code != 0:
                return f"Init:ExitCode:{exit_code}"
            # if exitCode == 0, continue checking subsequent init containers
            continue
        else:
            # Init container is running
            return f"Init:{i}/{len(init_statuses)}"
            
    # 3. Check App Containers status
    container_statuses = status.get("containerStatuses", [])
    for cs in container_statuses:
        state = cs.get("state", {})
        waiting = state.get("waiting", {})
        terminated = state.get("terminated", {})
        
        if waiting:
            reason = waiting.get("reason", "")
            return reason or "Waiting"
        elif terminated:
            reason = terminated.get("reason", "")
            if reason:
                return reason
            exit_code = terminated.get("exitCode", 0)
            if exit_code != 0:
                return f"ExitCode:{exit_code}"
            
    return phase

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

def check_is_aws_node(labels, node_name=None):
    region = labels.get("topology.kubernetes.io/region") or labels.get("failure-domain.beta.kubernetes.io/region")
    if region and ("us-east" in region or "us-west" in region or "eu-" in region or "ap-" in region or "sa-" in region or "ca-" in region or "me-" in region or "af-" in region):
        return True
    zone = labels.get("topology.kubernetes.io/zone") or labels.get("failure-domain.beta.kubernetes.io/zone")
    if zone and ("us-east" in zone or "us-west" in zone or "eu-" in zone or "ap-" in zone or "sa-" in zone or "ca-" in zone or "me-" in zone or "af-" in zone):
        return True
    for k, v in labels.items():
        if "aws" in k.lower() or "amazon" in k.lower() or "aws" in str(v).lower() or "amazon" in str(v).lower():
            return True
    if node_name and (node_name.startswith("i-") or "aws" in node_name.lower()):
        return True
    instance_type = labels.get("node.kubernetes.io/instance-type") or labels.get("beta.kubernetes.io/instance-type")
    if instance_type and re.match(r'^[a-z]+[0-9]+[a-z]*\.[a-z0-9]+$', instance_type):
        return True
    return False

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

def get_node_cost_details(labels, creation_ts, node_name, costs_map):
    if not check_is_aws_node(labels, node_name):
        return None
    instance_type = labels.get("node.kubernetes.io/instance-type") or labels.get("beta.kubernetes.io/instance-type")
    if not instance_type:
        return None
    hourly_cost = costs_map.get(instance_type)
    if hourly_cost is None:
        return None
    age_hours = 0.0
    if creation_ts:
        dt = parse_timestamp_to_datetime(creation_ts)
        if dt:
            now = datetime.datetime.now(datetime.timezone.utc)
            age_hours = max(0.0, (now - dt).total_seconds() / 3600.0)
    total_cost = round(hourly_cost * age_hours, 2)
    return {
        "provider": "aws",
        "datacenter": "us-east-1",
        "instanceType": instance_type,
        "hourlyCost": hourly_cost,
        "ageHours": round(age_hours, 1),
        "totalCost": total_cost
    }

def parse_cluster(context=None, force_mock=False):
    # Load AWS costs database
    script_dir = os.path.dirname(os.path.abspath(__file__))
    csv_path = os.path.join(script_dir, "supplements", "cost_data", "aws", "aws_ec2_us_east_1.csv")
    costs_map = load_aws_costs(csv_path)

    # 1. Gather data (live cluster pull or force mock)
    if force_mock:
        create_mock_files_if_missing()
    else:
        live_success = run_kubectl(context)
        if not live_success:
            if context:
                print(f"Error: Failed to query cluster context '{context}'")
                sys.exit(1)
            print("▲ Live cluster query skipped/failed. Processing via local files...")
            create_mock_files_if_missing()

    # 2. Read nodes
    if not os.path.exists(NODES_RAW_FILE):
        print(f"Error: {NODES_RAW_FILE} is missing. Cannot parse.")
        sys.exit(1)
        
    with open(NODES_RAW_FILE, "r", encoding="utf-8") as f:
        try:
            nodes_data = json.load(f)
        except json.JSONDecodeError as e:
            print(f"Error parsing {NODES_RAW_FILE}: {e}")
            sys.exit(1)

    # 3. Read pods
    if not os.path.exists(PODS_RAW_FILE):
        print(f"Error: {PODS_RAW_FILE} is missing. Cannot parse.")
        sys.exit(1)
        
    with open(PODS_RAW_FILE, "r", encoding="utf-8") as f:
        try:
            pods_data = json.load(f)
        except json.JSONDecodeError as e:
            print(f"Error parsing {PODS_RAW_FILE}: {e}")
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

        creation_ts = metadata.get("creationTimestamp")
        cost_details = get_node_cost_details(labels, creation_ts, name, costs_map)

        node_map[name] = {
            "name": name,
            "maxMemoryGB": max_mem_gb,
            "maxCPUCores": max_cpu_cores,
            "labels": labels,
            "conditions": conditions,
            "creationTimestamp": creation_ts,
            "costDetails": cost_details,
            "pods": [],
            "raw": node
        }

    # Process pods
    pod_list = pods_data.get("items", [])
    for pod in pod_list:
        metadata = pod.get("metadata", {})
        name = metadata.get("name")
        namespace = metadata.get("namespace", "default")
        
        spec = pod.get("spec", {})
        node_name = spec.get("nodeName")
        
        status = pod.get("status", {})
        phase = status.get("phase", "Unknown")

        # Filter out succeeded (completed) or failed pods, which release node memory
        if phase in ("Succeeded", "Failed"):
            continue

        # Skip unscheduled pods
        if not node_name:
            continue

        # Sum memory and CPU requests across all containers in the pod
        total_pod_memory_gb = 0.0
        total_pod_cpu_cores = 0.0
        containers = spec.get("containers", [])
        
        for c in containers:
            resources = c.get("resources", {})
            requests = resources.get("requests", {})
            limits = resources.get("limits", {})
            
            # Check requests.memory, fallback to limits.memory
            c_mem = requests.get("memory") or limits.get("memory")
            if c_mem:
                total_pod_memory_gb += parse_memory_to_gb(c_mem)
                
            # Check requests.cpu, fallback to limits.cpu
            c_cpu = requests.get("cpu") or limits.get("cpu")
            if c_cpu:
                total_pod_cpu_cores += parse_cpu_to_cores(c_cpu)
        
        # Fallback if no container memory requests/limits are specified.
        # Set to 0.1 GB (100Mi) to ensure pod still renders as a sleek 3D room.
        if total_pod_memory_gb <= 0.0:
            total_pod_memory_gb = 0.10
            
        if total_pod_cpu_cores <= 0.0:
            total_pod_cpu_cores = 0.10

        total_pod_memory_gb = round_to_one_decimal(total_pod_memory_gb)
        total_pod_cpu_cores = round_to_one_decimal(total_pod_cpu_cores)

        # Dynamic HSL hash-based color matching for the pod namespace
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
            "status": get_detailed_pod_status(pod),
            "namespace": namespace,
            "restarts": restarts,
            "creationTimestamp": metadata.get("creationTimestamp"),
            "raw": pod
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
                "pods": [],
                "raw": {
                    "apiVersion": "v1",
                    "kind": "Node",
                    "metadata": {
                        "name": node_name,
                        "labels": {
                            "kubernetes.io/hostname": node_name,
                            "kubernetes.io/os": "linux",
                            "note": "auto-created-placeholder"
                        }
                    },
                    "status": {
                        "allocatable": {
                            "memory": f"{placeholder_capacity}Gi",
                            "cpu": f"{placeholder_cpu}"
                        },
                        "capacity": {
                            "memory": f"{placeholder_capacity}Gi",
                            "cpu": f"{placeholder_cpu}"
                        },
                        "conditions": [
                            {"type": "Ready", "status": "True"},
                            {"type": "MemoryPressure", "status": "False"},
                            {"type": "DiskPressure", "status": "False"},
                            {"type": "PIDPressure", "status": "False"}
                        ]
                    }
                }
            }

        node_map[node_name]["pods"].append(pod_item)

    # Format the completed cluster configuration
    compiled_nodes = list(node_map.values())
    
    # Sort nodes alphabetically for structured rendering layout
    compiled_nodes.sort(key=lambda x: x["name"])

    cluster_name = get_cluster_name(context) or "ClusterNamePlaceHolder"

    output_state = {
        "clusterName": cluster_name,
        "nodes": compiled_nodes
    }

    # Write out Compiled cluster_state.json
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(output_state, f, indent=2)

    print(f"✔ Successfully parsed {len(node_list)} nodes and {len(pod_list)} pods.")
    print(f"✔ Compiled cluster environment state written to '{OUTPUT_FILE}' for cluster: {cluster_name}.")

if __name__ == "__main__":
    context = None
    if len(sys.argv) > 2 and sys.argv[1] == "--context":
        context = sys.argv[2]
    elif len(sys.argv) > 1 and sys.argv[1].startswith("--context="):
        context = sys.argv[1].split("=", 1)[1]
        
    if context and not re.match(r'^[a-zA-Z0-9_./:@-]+$', context):
        print(f"Error: Invalid context name format '{context}'", file=sys.stderr)
        sys.exit(1)
        
    parse_cluster(context)
