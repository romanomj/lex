# Project Vision: K8s 3D Cluster World

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

3. Front-End Ingestion (`lex.html`)
- Fetches `cluster_state.json` dynamically on initialization.
- Includes a CORS-safe catch-all block that falls back to embedded static demo data when opened directly via filesystem (`file://`) or when the compiled asset is missing.
- Features a connection status indicator (`● Live Cluster` vs `▲ Demo Mode`) in the HUD.

Dev Logs & Next Targets
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
- **Premium Top-Center Cluster Metrics Dashboard**: Designed and implemented an absolute top-center glassmorphic statistics panel displaying: active Kubernetes cluster name (retrieved dynamically from the context with automatic fallback to placeholder), total cumulative nodes count, total scheduled pods, and total cumulative allocatable node memory in GB. Updated both the backend parsing script `parse_cluster.py` and the front-end Three.js scene controller in `lex.html` for seamless live-and-mock integration.
- **Beachy Grid Layout & Dynamic Overflow**: Transitioned nodes from a single linear path to a centered 2D grid layout on the beach island. The buildings fill up to 9 columns horizontally (maintaining a pristine shoreline border) before wrapping into subsequent rows. If nodes exceed the beach boundary, they overflow into the ocean. The player spawn camera adjusts dynamically to position the user at a safe distance in front of the front-most row looking toward the cluster center.
- **Dynamic Divider/Partition Thickness for Tiny Pods**: Implemented dynamic scaling for horizontal floor partition slabs (dividing bars). When a pod or its adjacent element is significantly smaller than the node, the partition thickness is scaled down dynamically (down to a minimum of 0.01 units) to prevent the thin pod room layers from being visually swallowed, obscured, or overlapped by the divider bars.
- **Holographic Node Side Screens (Labels & Conditions)**: Implemented self-illuminating holographic side screens for every skyscraper node. The right-side screens render the node's Kubernetes metadata labels dynamically (auto-formatting, key truncation, and value text-wrapping in a monospace developer console console style). The left-side screens render health status conditions (e.g. `Ready`, `MemoryPressure`, `DiskPressure`, `PIDPressure`) with high-contrast glowing status alerts (e.g. healthy states glow in emerald green, while active warning pressure states glow in high-vibrancy warning red/amber with smooth canvas-level shadows). Fully updated both `parse_cluster.py` and the offline mock falls back database in `lex.html` for complete feature symmetry.
- **Dynamic Speed Controls**: Default spectator and movement speed set to twice its original value (base 200.0). Integrated keyboard speed control via "+"/"-" keys (increasing/decreasing by 10% increments) and introduced premium on-screen glassmorphic "+" and "-" speed control buttons with interactive hover/active states, active text scaling effects, and cursor lock prevention logic.
- **Scrollable Pod YAML Panel**: Implemented an automated inside-room detection system using real-time AABB coordinates (`THREE.Box3().containsPoint(camera.position)`). When the user physically enters any colorful pod room, standard raycasting suspends and the HUD card locks to the active pod room, while a new glassmorphic `#pod-yaml-panel` appears on the right side of the screen displaying the full syntax-highlighted Kubernetes Pod spec (compiled dynamically from the live ingestion pipeline or synthesized gracefully via an offline fallback engine). Integrated standard crosshair raycast hover events to automatically invoke and display this YAML panel from a distance, hiding it cleanly when hovering over node structural frames, unallocated space, or empty air. Added a highly efficient recursive `jsonToYaml` compiler with standard K8s elements sorted to the top, custom glassmorphic scrollbars, and interactive manual Up/Down scroll navigation buttons that bind click events to manual scroll overrides when the mouse is unlocked (via `ESC`).
- **Redesigned Failure States & Synchronized Blinking Red Animation**: Overhauled health alert styling for nodes and pods. Redesigned failed node materials to a rich, less-metallic, solid matte red (`roughness: 0.4`, `metalness: 0.1`, base color `0xef4444`) to eliminate dark reflections and preserve high color visibility. Implemented a highly efficient, CPU-friendly synchronized blinking/pulsing animation loop inside the `animate()` function. By sharing materials (`failedWallMaterial`, `failedOutlineMaterial`, `failedPodMaterial`, and `failedPodOutlineMaterial`) and interpolating their colors dynamically in the render cycle using a unified cosine/sine time-delta factor, all nodes under resource pressure and all failed pods pulse smoothly and in perfect synchrony between deep alert red and glowing warning red, completely avoiding costly scene graph traversals.
- **Floating Holographic Node Warning Signs**: Implemented rotating and floating 3D holographic exclamation mark warning signs directly above each and any skyscraper node that is in a non-Ready/pressure condition, or has a scheduled pod that has failed/CrashLoopBackOff. The signs scale to match the skyscrapers beautifully, featuring rotating gyroscopic status rings and red glow intensities that pulse smoothly in synchrony with the rest of the cluster alert system, providing immediate and exact spatial failure mapping.
- **Persistent Mouse Release Spec View**: Fixed a mouse release interaction issue where hitting ESC to unlock pointer lock (for scrollbar or speed adjustments) would prematurely clear the active Pod YAML and HUD panels. The raycaster hover detection has been decoupled from the pointer lock validation loop, allowing the panels to remain beautifully visible on screen as long as the center crosshair continues to rest on the selected pod, providing perfect context retention for standard mouse interaction.

Potential Future Requests
1. [x] **Fly Mode / Spectator Toggle**: Enabled flying up and down smoothly using `Space` and `Shift` keys, complete with dynamic spectator bounding physics. Fully integrated adjustable speed controls (default doubled, +/- 10% speed variations via UI buttons and hotkeys).
2. **Minimap or HUD Radar**: Add a 2D canvas overlay in the HUD showing a bird's-eye schematic map of the cluster layout, highlighting the player's current coordinate and looking direction.
3. **Collision Override Toggle**: Allow a debug key (e.g., `N` for noclip) to temporarily disable AABB collision detection, allowing rapid debugging or walking directly through walls.