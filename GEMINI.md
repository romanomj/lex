# Project Vision: Lex

An interactive, in-browser 3D topology visualizer that maps a Kubernetes cluster into a physical world layout (similar to a roofless Minecraft city). This architecture provides an intuitive, spatial look at hardware resource pressure and workload distributions.

## Core Concepts & Rules

- **Nodes = Buildings:** Every node in the cluster is a skyscraper structure with a fixed footprint. The total height of the building represents its total allocatable memory.
- **Pods = Rooms:** Each pod scheduled to a node is rendered as an individual room within that building. 
- **Resource Proportions:** Rooms are dynamically stacked as floor segments vertically. If a pod requests 12GB on a 24GB node, its floor room takes up exactly 50% of that building's internal height.
- **Under-utilization Visibility:** If total pod allocations don't equal 100% of node capacity, the remaining space is rendered as a clean, completely empty space at the top of the node, preserving smooth visuals. The empty space remains interactive and can be selected/hovered to view real-time available capacity metrics in the HUD.

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

[x] Hover/Look Tooltips: Cast a ray from the crosshair (Raycaster) to detect which pod room or node the user is staring at, displaying real-time metadata (Pod Name, Status, Namespace, Exact Memory Request) via an overlay box.

[x] Dynamic Player Spawning: Calculate physical boundaries of the generated buildings dynamically, spawning the player at a safe distance outside the cluster `(centerX, 2.5, -25)` looking at the cluster's center. This avoids AABB collision traps at startup and provides an open, immersive street-level entrance.

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

[x] Front-End Ingestion (`index.html`):
  - Fetches `cluster_state.json` dynamically on initialization.
  - Includes a CORS-safe catch-all block that falls back to embedded static demo data when opened directly via filesystem (`file://`) or when the compiled asset is missing.
  - Features a connection status indicator (`● Live Cluster` vs `▲ Demo Mode`) in the HUD.

Phase 3: Live View & Interactive Commands Engine (K9s-like Experience)
- [ ] **Lightweight Local API Server (`server.py`)**: Subclass `http.server.SimpleHTTPRequestHandler` to serve the web assets, run a background thread to poll the cluster context dynamically via `parse_cluster.py` every 30 seconds (configurable), and expose a modular REST API interface.
- [ ] **Near-Real-Time Synchronization**: Set up a lightweight polling or SSE client loop in `index.html` that receives the newest cluster state and implements a high-performance differential patcher in Three.js (adding, removing, or updating meshes smoothly with transitions rather than hard scene recreation).
- [ ] **Interactive Pod Command Menu ("C" Hotkey)**: Integrate key triggers so that hitting the "C" key while looking at a pod unlocks pointer controls and displays a floating, modern glassmorphic action menu listing read-only operations.
- [ ] **Modular Backend Command Handlers (Logs & Describe)**: Create reusable classes for invoking shell-level `kubectl` operations securely, sanitizing names and namespaces, and displaying output dynamically in a retro terminal-styled popup panel inside the browser canvas.

Dev Logs & Next Targets
- **Node Ready & Coming Online Visual Indicators**: Designed and implemented support for nodes currently being built or coming online (e.g. `Ready` is `Unknown` or initializing reasons). Created a beautiful, standard cyber-blue blinking material state and a dynamic gyroscopic, spinning holographic blue loader above the node roof, completely replacing warning/failure indicators for online-pending states. Integrated into both the ingestion script `parse_cluster.py` and the front-end Three.js scene fallbacks.
- **Collision Spawning Bug Fix**: Resolved a critical issue where players would spawn inside the physical structure of a node and get trapped due to the AABB colliders. Implemented dynamic bounding box calculation in `generateClusterWorld()` to offset the player safely to the front-center of the entire cluster at `Z = -25`, rotated 180 degrees to face the nodes directly.
- **Vertical Skyscraper Stacking Architecture**: Transitioned the visual world layout from horizontal depth-slicing to vertical skyscraper stacking. The maximum memory capacity of a node now dictates the overall height of its building structure, while scheduled pods are stacked vertically inside it as modern glassmorphic floor rooms. Horizontal partition slabs act as ceiling/floor layers, and unallocated/idle node capacity resides at the top of the building as a proportional clean, empty space. Sleek margins around all boxes prevent any rendering overlap (Z-fighting) with the outer walls.
- **Smooth Spectator Flight Physics**: Implemented seamless spectator flying controls (`Space` to float up, `Shift` to glide down) with dynamic, height-aware AABB collision detection. The camera can fly over buildings without boundary collisions but maintains realistic outer/partition wall collisions when walking or flying inside the rooms.
- **One-Decimal Precision & CPU Ingestion**: Implemented precise resource rounding to 1 decimal place (e.g., `48` or `32.5`) for Memory and CPU cores. Integrated full parsing for CPU limits/requests from pod containers and capacities from nodes, presenting them in a new, high-tech HUD overlay.
- **Dynamic Node Heights**: Fully implemented dynamic node height scaling proportional to each node's maximum Memory allotment (e.g., a 24GB node is twice as tall as a 12GB node).
- **Glassmorphic Semi-Transparent Outer Walls & Glowing Outlines**: Replaced flat solid gray outer walls and partitions with modern glassmorphic, semi-transparent blue/slate glass materials and added glowing holographic wireframe outlines to the building shells, making internal colorful pods perfectly visible from the outside.
- **Precision Collision Optimization**: Restructured collider construction to only include structural outer/partition walls, excluding internal pod volume boxes so players can seamlessly walk or fly inside pod rooms.
- **Optimization Pass**: Look into Three.js InstancedMesh if handling clusters scaling past 50+ nodes/1000+ pods to avoid draw-call bottlenecks.
- **Immersive Beach & Ocean Landscape**: Transformed the dark, empty void into a vibrant, high-fidelity beach island environment. Implemented a procedural canvas-based sand grain texture tiled across a massive beach island block. Added a glowing sun core with concentric billboarded lens-flare glow rings that dynamically track the camera's position. Created a real-time animated ocean surrounding the island, utilizing customized vertex plane displacement (sine/cosine wave functions) for fluid, shimmering water movement. Upgraded scene lighting with sky/ground hemispheric ambient color bouncing and warm solar directional shadowing.
- **Open-Front Dollhouse Node Design & Raycast Selection Fix**: Solved a key raycast blocking issue where the semi-transparent front wall of a node would intercept the selection crosshair, preventing hover detection of internal pods. Removed the front wall mesh and outline entirely to expose all pods from the front street-view, creating a beautiful open-faced "dollhouse" layout. Hovering directly over the colorful pods now presents real-time pod statistics, while viewing the buildings from the left, right, or back still displays exact node resource metrics.
- **Premium Slate-Charcoal & Slate-Gray Styling**: Updated the solid node wall shell material to a gorgeous dark slate-charcoal gray (`0x2c2e35`) and the horizontal floor partitions to a highly premium matching slate gray (`0x3a3d45`), replacing the older ultra-dark, almost black tones. This provides superior depth, visibility, and a sleek matte finish under the ambient sky/sand light and warm solar directional shadows.
- **Premium Top-Center Cluster Metrics Dashboard**: Designed and implemented an absolute top-center glassmorphic statistics panel displaying: active Kubernetes cluster name (retrieved dynamically from the context with automatic fallback to placeholder), total cumulative nodes count, total scheduled pods, and total cumulative allocatable node memory in GB. Updated both the backend parsing script `parse_cluster.py` and the front-end Three.js scene controller in `index.html` for seamless live-and-mock integration.
- **Beachy Grid Layout & Dynamic Overflow**: Transitioned nodes from a single linear path to a centered 2D grid layout on the beach island. The buildings fill up to 9 columns horizontally (maintaining a pristine shoreline border) before wrapping into subsequent rows. If nodes exceed the beach boundary, they overflow into the ocean. The player spawn camera adjusts dynamically to position the user at a safe distance in front of the front-most row looking toward the cluster center.
- **Dynamic Divider/Partition Thickness for Tiny Pods**: Implemented dynamic scaling for horizontal floor partition slabs (dividing bars). When a pod or its adjacent element is significantly smaller than the node, the partition thickness is scaled down dynamically (down to a minimum of 0.01 units) to prevent the thin pod room layers from being visually swallowed, obscured, or overlapped by the divider bars.
- **Holographic Node Side Screens (Labels & Conditions)**: Implemented self-illuminating holographic side screens for every skyscraper node. The right-side screens render the node's Kubernetes metadata labels dynamically (auto-formatting, key truncation, and value text-wrapping in a monospace developer console console style). The left-side screens render health status conditions (e.g. `Ready`, `MemoryPressure`, `DiskPressure`, `PIDPressure`) with high-contrast glowing status alerts (e.g. healthy states glow in emerald green, while active warning pressure states glow in high-vibrancy warning red/amber with smooth canvas-level shadows). Fully updated both `parse_cluster.py` and the offline mock falls back database in `index.html` for complete feature symmetry.
- **Dynamic Speed Controls**: Default spectator and movement speed set to twice its original value (base 200.0). Integrated keyboard speed control via "+"/"-" keys (increasing/decreasing by 10% increments) and introduced premium on-screen glassmorphic "+" and "-" speed control buttons with interactive hover/active states, active text scaling effects, and cursor lock prevention logic.
- **Scrollable Pod YAML Panel**: Implemented an automated inside-room detection system using real-time AABB coordinates (`THREE.Box3().containsPoint(camera.position)`). When the user physically enters any colorful pod room, standard raycasting suspends and the HUD card locks to the active pod room, while a new glassmorphic `#pod-yaml-panel` appears on the right side of the screen displaying the full syntax-highlighted Kubernetes Pod spec (compiled dynamically from the live ingestion pipeline or synthesized gracefully via an offline fallback engine). Integrated standard crosshair raycast hover events to automatically invoke and display this YAML panel from a distance, hiding it cleanly when hovering over node structural frames, unallocated space, or empty air. Added a highly efficient recursive `jsonToYaml` compiler with standard K8s elements sorted to the top, custom glassmorphic scrollbars, and interactive manual Up/Down scroll navigation buttons that bind click events to manual scroll overrides when the mouse is unlocked (via `ESC`).
- **Redesigned Failure States & Synchronized Blinking Red Animation**: Overhauled health alert styling for nodes and pods. Redesigned failed node materials to a rich, less-metallic, solid matte red (`roughness: 0.4`, `metalness: 0.1`, base color `0xef4444`) to eliminate dark reflections and preserve high color visibility. Implemented a highly efficient, CPU-friendly synchronized blinking/pulsing animation loop inside the `animate()` function. By sharing materials (`failedWallMaterial`, `failedOutlineMaterial`, `failedPodMaterial`, and `failedPodOutlineMaterial`) and interpolating their colors dynamically in the render cycle using a unified cosine/sine time-delta factor, all nodes under resource pressure and all failed pods pulse smoothly and in perfect synchrony between deep alert red and glowing warning red, completely avoiding costly scene graph traversals.
- **Floating Holographic Node Warning Signs**: Implemented rotating and floating 3D holographic exclamation mark warning signs directly above each and any skyscraper node that is in a non-Ready/pressure condition, or has a scheduled pod that has failed/CrashLoopBackOff. The signs scale to match the skyscrapers beautifully, featuring rotating gyroscopic status rings and red glow intensities that pulse smoothly in synchrony with the rest of the cluster alert system, providing immediate and exact spatial failure mapping.
- **Persistent Mouse Release Spec View**: Fixed a mouse release interaction issue where hitting ESC to unlock pointer lock (for scrollbar or speed adjustments) would prematurely clear the active Pod YAML and HUD panels. The raycaster hover detection has been decoupled from the pointer lock validation loop, allowing the panels to remain beautifully visible on screen as long as the center crosshair continues to rest on the selected pod, providing perfect context retention for standard mouse interaction.
- **Total Estimated Cluster Cost Metric**: Added a new "Estimated cost" field to the top-center glassmorphic stats dashboard, expanding the panel to four columns. It iterates over all active nodes and sums their costs dynamically using the same unified algorithm as the individual rear cost canvas panel (incorporating either the live server's parsed `costDetails` or dynamically calculating AWS rates based on labels, creation timestamps, and active rates), keeping the overall metrics up to date in real-time.

Potential Future Requests
1. [x] **Fly Mode / Spectator Toggle**: Enabled flying up and down smoothly using `Space` and `Shift` keys, complete with dynamic spectator bounding physics. Fully integrated adjustable speed controls (default doubled, +/- 10% speed variations via UI buttons and hotkeys).
2. [ ] **Real-Time Live View & Command Execution System (Phase 3 Plan)**: Implement localhost server, real-time polling sync, and "C" command key popup overlays.
3. [ ] **Minimap or HUD Radar**: Add a 2D canvas overlay in the HUD showing a bird's-eye schematic map of the cluster layout, highlighting the player's current coordinate and looking direction.
4. [ ] **Collision Override Toggle**: Allow a debug key (e.g., `N` for noclip) to temporarily disable AABB collision detection, allowing rapid debugging or walking directly through walls.

---

## Phase 3 Architectural Plan of Execution

To transition this application to a real-time cluster visualizer and introduce modular, interactive troubleshooting commands, we will construct a clean backend-bridge pattern. Below is the multi-step plan we will execute in subsequent sessions:

### Step 1: Lightweight Local API Server (`server.py`)
Create a zero-dependency Python server script `server.py` using Python's standard `http.server` library to serve files over `http://localhost:8000`.
- **Background Worker Thread**: Spawn an asynchronous background thread that executes the core parsing loop of `parse_cluster.py` every 30 seconds (configurable, preventing cluster overhead from frequent subprocess execution). It will maintain the current state of the active cluster context securely in memory or update a hot `cluster_state.json` on disk.
- **Local Loopback Bounding**: Hard-bind server listener to `127.0.0.1` only. This isolates K8s command execution strictly to local host users, preventing any external network entry.
- **Modular REST Router**:
  - Implement a regex-based API path router (`/api/v1/*`) to process custom endpoints.
  - Route `/api/v1/state` -> Returns hot cluster state coordinates and metadata.
  - Structure API handlers modularly (e.g., `PodActionHandler`, `NodeActionHandler`) to allow future expansions to `Deployments`, `Configs`, etc.

### Step 2: Modular Subprocess Command Runners
Implement secure, read-only K8s context interaction.
- **Secure Parameter Validation**: Enforce strict regex parameters `^[a-z0-9.-]+$` on all queried Pod Names and Namespaces before passing them to subprocesses to eliminate any command-injection vector.
- **Logs Endpoint (`GET /api/v1/pods/logs`)**:
  - Parameterized extraction of `pod` and `namespace`.
  - Executes: `kubectl logs <pod> -n <namespace> --tail=200`
  - Returns output as a plain text response wrapped in standard HTTP codes.
- **Describe Endpoint (`GET /api/v1/pods/describe`)**:
  - Parameterized extraction of `pod` and `namespace`.
  - Executes: `kubectl describe pod <pod> -n <namespace>`
  - Returns raw command stdout.
- **Extensible Commands Map**: Keep command registrations in a structured layout so that write/mutating actions (e.g. `delete`, `restart`) or resource types (e.g. `describe deployment`) can be integrated via simple dictionary entries later.

### Step 3: High-Performance Three.js Differential Sync
Upgrade `index.html` to sync its environment seamlessly in real-time.
- **Sync Loop**: Initiate a periodic fetch cycle (polling `/api/v1/state` every 10–15 seconds to align with the server's update cycle and conserve CPU).
- **Three-Way Scene Diff Algorithm**: Avoid hard page refreshes or scene clearance. Keep track of current meshes in `activeNodeMeshes` and `activePodMeshes` maps:
  - **Deletions**: If a node or pod is missing in the new API payload, trigger a beautiful visual fade-out, remove bounding collision objects from the movement Loop's `colliders` list, dispose of geometries/materials to prevent memory leaks, and remove the meshes from the scene.
  - **Additions**: Build and transition-in meshes for new nodes/pods at their respective skyscraper floor offsets, generating side status cards and floating warning signs automatically.
  - **Updates**: If a pod's status changes (e.g., healthy to failed), transition its color or trigger its pulsing alert state immediately. If resources change, dynamically adjust room height values and slide stacked floors above them seamlessly.
- **HUD Retainment**: Preserve active HUD selection metrics and pointer lock camera vectors during synchronization updates.

### Step 4: "C" Command Menu UI & Retro Terminal Overlay
Implement interactive menus inside the 3D visual layer.
- **Hotkey Registration**: Intercept keypresses of the `C` key inside `index.html`.
- **AABB Hover Validation**: If the crosshair raycaster detects a valid pod room, trigger the context overlay flow:
  1. Capture pod name and namespace from the active mesh data storage.
  2. Suspend standard Pointer Lock (`controls.unlock()`) to bring back the mouse cursor.
  3. Render a floating glassmorphic `#pod-command-menu` option panel at the screen center showing button choices: `[📜 Get Logs]`, `[🔍 Describe Pod]`, `[⚙️ Restart Pod (Disabled)]`, `[❌ Delete Pod (Disabled)]`.
- **Glassmorphic Terminal Popup (`#command-output-modal`)**:
  - Clicking `Get Logs` or `Describe Pod` queries the local API server and pops open a modern terminal card overlay.
  - Features:
    - Glowing top bar with code-safe header detailing active command: `$ kubectl logs <pod> -n <namespace>`.
    - Compact monospace text container displaying output with styled lines (coloring error messages red and status highlights amber).
    - Custom slim scrollbars and quick-copy action buttons.
    - Large glassmorphic `[X Close]` button (or ESC key binding) that safely closes the overlay and automatically requests permission to re-engage the FPS Pointer Lock.
- **Modular Actions Array**:
  - The menu buttons and callbacks are driven by a clean, declarative JS config array. Adding new endpoints or UI options in future sessions requires only a single addition to the config array.