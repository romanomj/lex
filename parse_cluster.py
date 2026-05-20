#!/usr/bin/env python3
import os
import sys
import json
import re
import subprocess
import hashlib

# Configuration
NODES_RAW_FILE = "raw-nodes.json"
PODS_RAW_FILE = "raw-pods.json"
OUTPUT_FILE = "cluster_state.json"

# Sleek, premium modern palette for namespace colors (to avoid basic primary colors)
NAMESPACE_COLORS = [
    0x3b82f6,  # Indigo/Sleek Blue
    0x10b981,  # Emerald Green
    0x8b5cf6,  # Purple/Violet
    0xec4899,  # Rose Pink
    0xf59e0b,  # Warm Amber
    0x06b6d4,  # Vivid Cyan
    0x14b8a6,  # Modern Teal
    0xf43f5e,  # Rose Red
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

def run_kubectl():
    """Attempts to run kubectl to fetch live cluster JSONs."""
    print("Attempting to query active Kubernetes cluster context...")
    try:
        # Check if kubectl command exists
        subprocess.run(["kubectl", "version", "--client"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        print("▲ Note: 'kubectl' CLI utility is not installed or not in system PATH.")
        return False

    try:
        # Check active cluster connection
        subprocess.run(["kubectl", "config", "current-context"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
    except subprocess.CalledProcessError:
        print("▲ Note: 'kubectl' is installed, but no active cluster context was detected.")
        return False

    # Fetch nodes
    try:
        print(f"Fetching nodes list and writing to {NODES_RAW_FILE}...")
        nodes_res = subprocess.run(["kubectl", "get", "nodes", "-o", "json"], capture_output=True, text=True, check=True)
        with open(NODES_RAW_FILE, "w", encoding="utf-8") as f:
            f.write(nodes_res.stdout)
    except subprocess.CalledProcessError as e:
        print(f"▲ Error fetching nodes: {e.stderr}")
        return False

    # Fetch pods
    try:
        print(f"Fetching pods list across all namespaces and writing to {PODS_RAW_FILE}...")
        pods_res = subprocess.run(["kubectl", "get", "pods", "--all-namespaces", "-o", "json"], capture_output=True, text=True, check=True)
        with open(PODS_RAW_FILE, "w", encoding="utf-8") as f:
            f.write(pods_res.stdout)
    except subprocess.CalledProcessError as e:
        print(f"▲ Error fetching pods: {e.stderr}")
        return False

    print("● Live cluster data successfully extracted!")
    return True

def get_cluster_name():
    """Attempts to fetch active Kubernetes context/cluster name."""
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
    
    mock_nodes = {
        "apiVersion": "v1",
        "kind": "List",
        "items": [
            {
                "metadata": {
                    "name": "node-alpha",
                    "labels": {
                        "kubernetes.io/hostname": "node-alpha",
                        "kubernetes.io/os": "linux",
                        "kubernetes.io/arch": "amd64",
                        "node.kubernetes.io/instance-type": "t3.large",
                        "environment": "production"
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
                    "labels": {
                        "kubernetes.io/hostname": "node-bravo",
                        "kubernetes.io/os": "linux",
                        "kubernetes.io/arch": "amd64",
                        "node.kubernetes.io/instance-type": "t3.medium",
                        "environment": "database"
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
                    "labels": {
                        "kubernetes.io/hostname": "node-charlie",
                        "kubernetes.io/os": "linux",
                        "kubernetes.io/arch": "amd64",
                        "node.kubernetes.io/instance-type": "m5.xlarge",
                        "environment": "analytics"
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
            }
        ]
    }
    
    mock_pods = {
        "apiVersion": "v1",
        "kind": "List",
        "items": [
            {
                "metadata": {"name": "frontend-pod-7d4f9b8c-2x9v4", "namespace": "production"},
                "spec": {"nodeName": "node-alpha", "containers": [{"resources": {"requests": {"memory": "12Gi"}}}]},
                "status": {"phase": "Running"}
            },
            {
                "metadata": {"name": "backend-pod-5c8e7a1b-9p3k8", "namespace": "production"},
                "spec": {"nodeName": "node-alpha", "containers": [{"resources": {"requests": {"memory": "4Gi"}}}]},
                "status": {"phase": "Running"}
            },
            {
                "metadata": {"name": "cache-pod-9a3f2c7d-5m1q9", "namespace": "production"},
                "spec": {"nodeName": "node-alpha", "containers": [{"resources": {"requests": {"memory": "4Gi"}}}]},
                "status": {"phase": "Pending"}
            },
            {
                "metadata": {"name": "db-pod-0-8b6d4c2e-4w8z7", "namespace": "database"},
                "spec": {"nodeName": "node-bravo", "containers": [{"resources": {"requests": {"memory": "8Gi"}}}]},
                "status": {"phase": "Running"}
            },
            {
                "metadata": {"name": "db-pod-1-8b6d4c2e-5x9a2", "namespace": "database"},
                "spec": {"nodeName": "node-bravo", "containers": [{"resources": {"requests": {"memory": "8Gi"}}}]},
                "status": {"phase": "Running"}
            },
            {
                "metadata": {"name": "analytics-worker-6f9e8d7c-8y2v4", "namespace": "analytics"},
                "spec": {"nodeName": "node-charlie", "containers": [{"resources": {"requests": {"memory": "16Gi"}}}]},
                "status": {"phase": "Running"}
            },
            {
                "metadata": {"name": "logging-agent-3a5b7c9d-1r4q8", "namespace": "kube-system"},
                "spec": {"nodeName": "node-charlie", "containers": [{"resources": {"requests": {"memory": "4Gi"}}}]},
                "status": {"phase": "Running"}
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

def parse_cluster():
    # 1. Gather data (live cluster pull)
    live_success = run_kubectl()
    if not live_success:
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

        node_map[name] = {
            "name": name,
            "maxMemoryGB": max_mem_gb,
            "maxCPUCores": max_cpu_cores,
            "labels": labels,
            "conditions": conditions,
            "pods": []
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

        pod_item = {
            "name": name,
            "memoryGB": total_pod_memory_gb,
            "cpuCores": total_pod_cpu_cores,
            "color": color_int,
            "status": phase,
            "namespace": namespace
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
                "pods": []
            }

        node_map[node_name]["pods"].append(pod_item)

    # Format the completed cluster configuration
    compiled_nodes = list(node_map.values())
    
    # Sort nodes alphabetically for structured rendering layout
    compiled_nodes.sort(key=lambda x: x["name"])

    cluster_name = get_cluster_name() or "ClusterNamePlaceHolder"

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
    parse_cluster()
