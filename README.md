# Lex 🌐🧱

An interactive, in-browser 3D topology visualizer that maps a Kubernetes cluster into a physical world layout (similar to a roofless Minecraft city). This architecture provides an intuitive, spatial look at hardware resource pressure and workload distributions.

Nodes are represented as **buildings** (proportional to total allocatable memory), scheduled pods are represented as **segmented rooms**, and under-utilization is highlighted as an empty, gray **Available Capacity** wireframe room.

---

## ⚡ Zero-Dependency Architecture
Lex is engineered to be **extremely lightweight and portable**:
- **Backend & Ingestion**: 100% written in Python using only standard library modules (`http.server`, `subprocess`, `csv`, etc.). **No pip installs or external Python packages required!**
- **Frontend**: Single-file standard `index.html` entrypoint loading Three.js directly via native ES module imports.

---

## 🚀 Quick Start (Recommended)

You can launch the full, live-syncing Lex environment with a single command:

1. **Launch the Lex Local API Server:**
   ```bash
   python server.py
   ```
   *Lex will automatically bootstrap: it queries your active Kubernetes context if present, falls back gracefully to a beautiful mock cluster if offline, runs a background worker to sync changes every 30 seconds (`--interval`), and hosts the visualizer. Use `--port` to change the port and `--dvr-retention-days` (default 7, `0` = keep forever) to control how long DVR recordings are kept.*

2. **Open the Visualizer:**
   Navigate your browser to:
   [http://localhost:8000/](http://localhost:8000/)
   *The page will automatically load the visualizer right at the home page URL root!*

## 🛠️ Step-by-Step Setup

### Prerequisites
- **Python 3.x** (for the data ingestion pipeline)
- **A modern web browser** supporting ES modules (Chrome, Edge, Firefox, Safari)
- **kubectl** (Optional: only needed if you want to extract live cluster data)

---

The visualizer ingests data via a decoupled approach where raw JSON configurations are parsed into a single web asset, `cluster_state.json`. Lex redacts literal env values (often credentials) and `last-applied-configuration` before any dump is written, and keeps `data/` readable only by you. All of these files live in the `data/` directory (override with `LEX_DATA_DIR`), which is git-ignored and never served over HTTP, because it contains full pod specs.

```
[ Active K8s Cluster ]  ──( kubectl )──> [ raw-nodes.json ]
                                         [ raw-pods.json  ] ──> [ parse_cluster.py ] ──> [ cluster_state.json ] ──> [ index.html ]
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
If you do not have an active Kubernetes cluster, you can easily mock or inspect specific topologies by manually creating `data/raw-nodes.json` and `data/raw-pods.json`. Hand-written files are never overwritten; only fixtures Lex generated itself are refreshed:

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

### 1. Launching via Local API Server (Recommended)
Because standard web browsers restrict AJAX requests on raw file structures (CORS policy), serving the visualizer and loading data requires a server:

```bash
# Start our local API server in the project directory:
python server.py
```
Open [http://localhost:8000/](http://localhost:8000/) in your browser.
- The home page automatically serves `index.html`.
- Displays the status badge (`● Live Cluster`, `● Static File`, or `▲ Demo Mode` depending on connection status).
- Enlists our background context synchronizer to pull K8s updates every 30 seconds.
- Enables the "C" hotkey menu to securely view live logs, describe pods, check events, and inspect pod specifications.
- Only answers requests addressed to `127.0.0.1` / `localhost` on its own port, serves nothing but `index.html` and `images/`, and rejects cross-origin POSTs. Open it at `http://127.0.0.1:8000/` or `http://localhost:8000/`.

### 2. Launching in Zero-Server Offline Demo Mode
If you double-click `index.html` directly from your file explorer (triggering `file://` protocol in the browser):
- The browser blocks local JSON fetch due to CORS safety rules.
- The visualizer's built-in **CORS Catch-all block** intercepts the error, falling back to pre-packaged offline demo data.
- Displays a warm amber status badge `▲ Demo Mode` showing everything fully functional in offline simulation.

---

## 🕹️ Controls & Navigation

- **Click Screen:** Lock mouse look (FPS style).
- **WASD / Arrow Keys:** Walk around the cluster city.
- **Mouse Look:** Look up, down, and around.
- **K Key:** Switch cluster / kubectl context.
- **F Key:** Search the cluster.
- **O Key:** Ops panel: **Alerts** (click to fly to the affected node, pod or workload), **Vitals** (CPU and memory requests, pods by state, restarts, nodes and run-rate history over 1h–7d, with a table view) and **Top 5** (restarting pods, fullest nodes, namespaces by memory and cost). The same vitals are on the **Jumbotron** floating above the city; look up from the spawn point.
- **C Key:** Actions menu for the pod or node under the crosshair.
- **Shareable links:** press `L` to copy a link to the pod, node or view you're looking at (also in the `C` menu and on every alert). Opening it in Lex switches to the right cluster and flies there, which is handy for incident channels.
- **What-if failures:** press `C` on a building → *Simulate Node Failure*, or use Ops → Resilience to lose a node or a whole zone. Lost buildings turn to ghosts, rescheduled load appears in the surviving buildings, and pods that don't fit sail to the Waiting Lobby. The same tab lists single points of failure (all replicas on one node or zone, drain-blocking PDBs, single-replica workloads).
- **X Key:** Security X-ray: rooms colored by their worst security finding (privileged, host access, secrets in env vars, root…). Details in Ops → Security.
- **N Key:** Toggle noclip (walk through walls).
- **ESC Key:** Exit mouse lock / release cursor.
- **Crosshair Raycaster:** Point the center screen crosshair directly at any Pod Floor or Node Wall to see a real-time HUD Card pop up in the top right containing real-time metadata (Status, Namespace, Memory Request, Capacity Utilization).

---

## 🎨 Visual Design System

- **Namespace Hashing:** The visualizer automatically calculates an HSL color based on the MD5 hash of each pod's namespace. This ensures that pods in the same namespace (e.g., `production`, `kube-system`, `database`) share a matching floor visual style, making cluster boundaries instantly recognizable.
- **Resource Proportions:** A pod requesting 12GB on a 24GB node takes up precisely 50% of the interior area of that building.
- **Waiting Lobby:** Pods the scheduler can't place wait as crates at a dock on the island's left shore, sized by memory request and tinted by namespace. Newly pending pods arrive by boat, which sails in, unloads at the pier and leaves. Crates that have waited more than 5 minutes pulse amber. Failed and Evicted pods become rubble in front of the node they ran on. Hover for the scheduler's reason, press `C` for describe/events, or click the edge marker or the "Unscheduled" stat to fly to the dock.
- **State Badging:** Extended CSS styles map standard Kubernetes pod states (`Running`, `Pending`, `Succeeded`, `Completed`, `Error`, `CrashLoopBackOff`) to distinct, beautiful glassmorphism status colors.
