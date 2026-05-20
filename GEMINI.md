# Project Vision: K8s 3D Cluster World

An interactive, in-browser 3D topology visualizer that maps a Kubernetes cluster into a physical world layout (similar to a roofless Minecraft city). This architecture provides an intuitive, spatial look at hardware resource pressure and workload distributions.

## Core Concepts & Rules

- **Nodes = Buildings:** Every node in the cluster is an open-roof building. The total length/footprint of the building represents its total allocatable memory.
- **Pods = Rooms:** Each pod scheduled to a node is rendered as an individual room within that building. 
- **Resource Proportions:** Rooms are dynamically sliced along the floor grid. If a pod requests 12GB on a 24GB node, its room boundaries take up exactly 50% of that building's internal area.
- **Under-utilization Visibility:** If total pod allocations don't equal 100% of node capacity, the remaining space renders as an empty "Available Capacity" gray wireframe room.

---

## Current Architecture Blueprint

The project relies on a completely lightweight, zero-heavy-engine web stack for rapid loading directly in standard browsers:

### Front-End Web Engine
- **Rendering Layer:** Three.js (via native ES modules) handling primitives, lighting, and performance-friendly box materials.
- **Navigation Controls:** `PointerLockControls` enabling mouse-look and standard FPS keyboard navigation (WASD / Arrow Keys).
- **Layout Math:** Procedural 1D axis slicing along an optimized bounding depth axis based on normalized resource integers.

The Normalization Strategy: Kubernetes returns messy raw strings (64Mi, 2Gi, 2G). The pipeline must transform all values into a single integer unit (Megabytes or Gigabytes) before calculating 3D object geometry.

Ongoing Implementation Roadmap
Phase 1: Interactive Polish (3D Visual Layer)
[x] AABB Box Collision Detection: Implement basic axis-aligned bounding box rules (within the character movement loop) to prevent the user's camera from walking through building outer walls and room partitions.

[x] Hover/Look Tooltips: Cast a ray from the crosshair (Raycaster) to detect which pod room or node building the user is staring at, displaying real-time metadata (Pod Name, Status, Namespace, Exact Memory Request) via an overlay box.

Phase 2: Live Cluster Data Integration
To transition the world from static mock JSON data to a live infrastructure view, the pipeline uses a decoupled ingestion approach that processes data externally:

1. Local Processing Engine (CLI & Python Ingest Pipeline)
A unified Python pipeline (`parse_cluster.py`) has been added to extract and compile cluster data:
- **Live Ingest:** Queries the active cluster using `kubectl` (if context is active) to extract `raw-nodes.json` and `raw-pods.json`.
- **Auto-Bootstrapping / Offline Fallback:** Automatically generates standard mock files if run offline without an active cluster context to ensure immediate testability.

2. Parsing & Compilation Pipeline
[x] Data Assembly Script (`parse_cluster.py`):
  - Matches each pod's `spec.nodeName` to its scheduled building.
  - Sums memory requests (`spec.containers[].resources.requests.memory`) with a robust fallback from requests to limits, and to a minimum baseline (`100Mi`) to keep all active workloads visible.
  - Hashes pod namespaces to dynamic, curated HSL colors so that identical namespaces share matching visual room styles.
  
[x] String Unit Parser:
  - Robust regex mapper converts K8s text formats (`Ki` / `M` / `Mi` / `Gi` / `G` / raw bytes) into normalized floating-point Gigabytes.

[x] Static Asset Compilation:
  - Compiles the aligned structures into a standalone `cluster_state.json` file in the web folder.

3. Front-End Ingestion (`lex.html`)
- Fetches `cluster_state.json` dynamically on initialization.
- Includes a CORS-safe catch-all block that falls back to embedded static demo data when opened directly via filesystem (`file://`) or when the compiled asset is missing.
- Features a connection status indicator (`● Live Cluster` vs `▲ Demo Mode`) in the HUD.

Dev Logs & Next Targets
Optimization Pass: Look into Three.js InstancedMesh if handling clusters scaling past 50+ nodes/1000+ pods to avoid draw-call bottlenecks.

Expansion: Think about mapping CPU limits to wall heights in future sprints, transforming 2D floor areas into dynamic 3D room volumes!