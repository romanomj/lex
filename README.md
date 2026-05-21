# Lex 🌐🧱

An interactive, in-browser 3D topology visualizer that maps a Kubernetes cluster into a physical world layout (similar to a roofless Minecraft city). This architecture provides an intuitive, spatial look at hardware resource pressure and workload distributions.

Nodes are represented as **buildings** (proportional to total allocatable memory), scheduled pods are represented as **segmented rooms**, and under-utilization is highlighted as an empty, gray **Available Capacity** wireframe room.

---

## 🚀 Quick Start (Offline Mode)

You can launch and explore the visualizer immediately in **Demo Mode** without an active Kubernetes cluster:

1. **Bootstrap the cluster state:**
   ```bash
   python parse_cluster.py
   ```
   *Note: Since no live Kubernetes context is detected, this script will automatically generate sample `raw-nodes.json` and `raw-pods.json` files and compile them into `cluster_state.json` for you.*

2. **Start a local development server:**
   ```bash
   python -m http.server 8000
   ```

3. **Open the visualizer:**
   Navigate your browser to `http://localhost:8000/lex.html`.
   *You will see the visualizer load in **Live Cluster** mode (emerald badge) rendering the parsed state!*

---

## 🛠️ Step-by-Step Setup

### Prerequisites
- **Python 3.x** (for the data ingestion pipeline)
- **A modern web browser** supporting ES modules (Chrome, Edge, Firefox, Safari)
- **kubectl** (Optional: only needed if you want to extract live cluster data)

---

## 📊 Generating Cluster Configurations

The visualizer ingests data via a decoupled approach where raw JSON configurations are parsed into a single web asset, `cluster_state.json`.

```
[ Active K8s Cluster ]  ──( kubectl )──> [ raw-nodes.json ]
                                         [ raw-pods.json  ] ──> [ parse_cluster.py ] ──> [ cluster_state.json ] ──> [ lex.html ]
[ Manual JSON Files  ]  ────────────────>
```

### Option A: Extracting Live Cluster Data (Online)
If you have an active Kubernetes cluster, configure your `kubectl` context, and run:
```bash
python parse_cluster.py
```
The script will automatically detect your client, query the cluster, and output:
- `raw-nodes.json` (raw API output of your node topology)
- `raw-pods.json` (raw API output of all running/pending pods across namespaces)
- `cluster_state.json` (the compiled visualizer asset)

### Option B: Generating Pod & Node Configurations Manually (Offline)
If you do not have an active Kubernetes cluster, you can easily mock or inspect specific topologies by manually creating `raw-nodes.json` and `raw-pods.json` in the directory:

#### 1. Define Nodes (`raw-nodes.json`)
Create `raw-nodes.json` with the following structure. You can use standard Kubernetes memory strings (`Ki`, `Mi`, `Gi`, `G`, `M`, or raw bytes):
```json
{
  "apiVersion": "v1",
  "kind": "List",
  "items": [
    {
      "metadata": { "name": "node-alpha" },
      "status": {
        "allocatable": { "memory": "24Gi" }
      }
    },
    {
      "metadata": { "name": "node-bravo" },
      "status": {
        "allocatable": { "memory": "16Gi" }
      }
    }
  ]
}
```

#### 2. Define Pods (`raw-pods.json`)
Create `raw-pods.json` mapping pods to the corresponding `spec.nodeName` host. Container resource requests or limits can be in varying formats:
```json
{
  "apiVersion": "v1",
  "kind": "List",
  "items": [
    {
      "metadata": {
        "name": "frontend-pod-7d4f9b",
        "namespace": "production"
      },
      "spec": {
        "nodeName": "node-alpha",
        "containers": [
          { "resources": { "requests": { "memory": "12Gi" } } }
        ]
      },
      "status": { "phase": "Running" }
    },
    {
      "metadata": {
        "name": "cache-pod-9a3f2c",
        "namespace": "production"
      },
      "spec": {
        "nodeName": "node-alpha",
        "containers": [
          { "resources": { "requests": { "memory": "4096Mi" } } }
        ]
      },
      "status": { "phase": "Pending" }
    }
  ]
}
```

#### 3. Compile the State
Once your raw JSON files are ready, compile them:
```bash
python parse_cluster.py
```
This produces the fully compiled, highly optimized `cluster_state.json`.

---

## 🎮 Launching the Visualizer

### 1. Launching with Live Data (Recommended)
Because standard web browsers restrict AJAX requests on raw file structures (CORS policy), loading the dynamically compiled `cluster_state.json` requires a lightweight web server:

```bash
# In the project directory, launch a web server:
python -m http.server 8000
```
Open `http://localhost:8000/lex.html` in your browser. 
- You will see the **Live Cluster** badge indicating active connection.
- All building layouts, pod dimensions, and namespace colors reflect your `cluster_state.json` exactly.

### 2. Launching in Offline Demo Mode (Zero Server)
If you simply double-click `lex.html` directly from your file system (triggering `file://` protocol):
- The browser will block the local JSON fetch due to CORS safety.
- The visualizer's built-in **CORS Catch-all block** will gracefully intercept this error and fallback to rendering pre-packaged static demo data.
- The instructions HUD will render a warm amber badge stating `▲ Demo Mode (Static)` showing that everything is fully functional.

---

## 🕹️ Controls & Navigation

- **Click Screen:** Lock mouse look (FPS style).
- **WASD / Arrow Keys:** Walk around the cluster city.
- **Mouse Look:** Look up, down, and around.
- **ESC Key:** Exit mouse lock / release cursor.
- **Crosshair Raycaster:** Point the center screen crosshair directly at any Pod Floor or Node Wall to see a real-time HUD Card pop up in the top right containing real-time metadata (Status, Namespace, Memory Request, Capacity Utilization).

---

## 🎨 Visual Design System

- **Namespace Hashing:** The visualizer automatically calculates an HSL color based on the MD5 hash of each pod's namespace. This ensures that pods in the same namespace (e.g., `production`, `kube-system`, `database`) share a matching floor visual style, making cluster boundaries instantly recognizable.
- **Resource Proportions:** A pod requesting 12GB on a 24GB node takes up precisely 50% of the interior area of that building.
- **State Badging:** Extended CSS styles map standard Kubernetes pod states (`Running`, `Pending`, `Succeeded`, `Completed`, `Error`, `CrashLoopBackOff`) to distinct, beautiful glassmorphism status colors.
