#!/usr/bin/env python3
"""
Lightweight Local API Server for Lex.
Serves web assets on http://127.0.0.1:8000, runs a background thread to
periodically query Kubernetes context, and exposes secure endpoints for pod troubleshooting.
"""

import os
import re
import json
import time
import subprocess
import threading
import http.server
from urllib.parse import urlparse, parse_qs
import parse_cluster

PORT = 8000
BIND_ADDRESS = '127.0.0.1'  # Hard-bound to local loopback for secure sandbox isolation

# Thread-safe context management
active_context = "demo"
active_context_lock = threading.Lock()

class LocalAPIServer(http.server.SimpleHTTPRequestHandler):
    def end_headers(self):
        # Enable CORS for local convenience and disable caching for API endpoints
        self.send_header('Access-Control-Allow-Origin', '*')
        if self.path.startswith('/api/v1/'):
            self.send_header('Cache-Control', 'no-store, no-cache, must-revalidate, max-age=0')
        super().end_headers()

    def do_GET(self):
        parsed_url = urlparse(self.path)
        path = parsed_url.path

        # Route: API state fetch
        if path == '/api/v1/state':
            self.handle_get_state()
        # Route: API contexts fetch
        elif path == '/api/v1/contexts':
            self.handle_get_contexts()
        # Route: API logs fetch
        elif path == '/api/v1/pods/logs':
            self.handle_pod_logs(parsed_url.query)
        # Route: API pod describe
        elif path == '/api/v1/pods/describe':
            self.handle_pod_describe(parsed_url.query)
        # Route: API pod spec
        elif path == '/api/v1/pods/spec':
            self.handle_pod_spec(parsed_url.query)
        # Route: API node spec
        elif path == '/api/v1/nodes/spec':
            self.handle_node_spec(parsed_url.query)
        # Route: API events fetch
        elif path == '/api/v1/events':
            self.handle_get_events(parsed_url.query)
        else:
            # Fallback to serving static files (index.html, etc.) from the workspace directory
            super().do_GET()

    def do_POST(self):
        parsed_url = urlparse(self.path)
        path = parsed_url.path

        if path == '/api/v1/contexts/switch':
            self.handle_switch_context()
        else:
            self.send_error_json(404, "Endpoint not found")

    def handle_get_state(self):
        state_file = parse_cluster.OUTPUT_FILE
        with active_context_lock:
            ctx = active_context

        if not os.path.exists(state_file):
            print("State file missing, generating synchronously...")
            try:
                if ctx == "demo":
                    parse_cluster.parse_cluster(force_mock=True)
                else:
                    parse_cluster.parse_cluster(context=ctx)
            except SystemExit:
                self.send_error_json(502, f"Failed to generate cluster state: Context '{ctx}' is unreachable.")
                return
            except Exception as e:
                self.send_error_json(500, f"Error generating cluster state: {str(e)}")
                return

        if os.path.exists(state_file):
            try:
                with open(state_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                data["activeContext"] = ctx
                self.send_json_response(200, data)
            except Exception as e:
                self.send_error_json(500, f"Error reading state file: {str(e)}")
        else:
            self.send_error_json(404, "Cluster state file could not be found.")

    def handle_get_contexts(self):
        try:
            res = subprocess.run(["kubectl", "config", "get-contexts", "-o", "name"], capture_output=True, text=True, timeout=3)
            contexts = []
            if res.returncode == 0:
                contexts = [line.strip() for line in res.stdout.split('\n') if line.strip()]
            
            if "demo" not in contexts:
                contexts = ["demo"] + contexts
                
            with active_context_lock:
                ctx = active_context
                
            self.send_json_response(200, {
                "contexts": contexts,
                "activeContext": ctx
            })
        except Exception as e:
            self.send_json_response(200, {
                "contexts": ["demo"],
                "activeContext": "demo",
                "warning": f"Could not query host contexts: {str(e)}"
            })

    def handle_switch_context(self):
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            post_data = self.rfile.read(content_length)
            data = json.loads(post_data.decode('utf-8'))
        except Exception as e:
            self.send_error_json(400, f"Invalid JSON payload: {str(e)}")
            return

        target_context = data.get("context")
        if not target_context:
            self.send_error_json(400, "Missing 'context' field in payload")
            return

        if target_context != "demo" and not re.match(r"^[a-zA-Z0-9_./:@-]+$", target_context):
            self.send_error_json(400, "Invalid context name format")
            return

        global active_context
        if target_context == "demo":
            try:
                parse_cluster.parse_cluster(force_mock=True)
                with active_context_lock:
                    active_context = "demo"
                self.send_json_response(200, {"status": "success", "activeContext": "demo"})
            except Exception as e:
                self.send_error_json(500, f"Failed to switch to Demo Mode: {str(e)}")
        else:
            try:
                parse_cluster.parse_cluster(context=target_context)
                with active_context_lock:
                    active_context = target_context
                self.send_json_response(200, {"status": "success", "activeContext": target_context})
            except SystemExit:
                self.send_error_json(502, f"Failed to query cluster context '{target_context}'. Cluster may be offline or unreachable.")
            except Exception as e:
                self.send_error_json(500, f"Unexpected error during context switch: {str(e)}")

    def handle_pod_logs(self, query_string):
        params = parse_qs(query_string)
        pod = params.get('pod', [None])[0]
        namespace = params.get('namespace', [None])[0]

        if not pod or not namespace:
            self.send_error_json(400, "Missing required query parameters: 'pod' and 'namespace'")
            return

        if not re.match(r"^[a-z0-9.-]+$", pod) or not re.match(r"^[a-z0-9.-]+$", namespace):
            self.send_error_json(400, "Invalid characters in pod or namespace parameter")
            return

        with active_context_lock:
            ctx = active_context

        if ctx == "demo":
            mock_logs = f"""[DEMO MODE ACTIVE] - Streaming logs for pod '{pod}' in namespace '{namespace}'
2026-05-22T10:00:00Z INFO [app] Initializing container...
2026-05-22T10:00:02Z INFO [app] Database connection pool established (10 connections).
2026-05-22T10:00:05Z INFO [app] Server listening on :8080
2026-05-22T10:05:00Z WARN [app] Request latency spike detected (duration: 350ms)
2026-05-22T10:15:32Z INFO [app] Garbage collection completed (released 42MB)
2026-05-22T10:45:12Z DEBUG [app] Active websocket connections: 24"""
            self.send_text_response(200, mock_logs)
            return

        try:
            print(f"Running secure command: kubectl logs {pod} -n {namespace} --tail=200 (context: {ctx})")
            cmd = ["kubectl", "--context", ctx, "logs", pod, "-n", namespace, "--tail=200"]
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            if res.returncode == 0:
                self.send_text_response(200, res.stdout)
            else:
                error_msg = res.stderr or "Unknown error fetching logs"
                self.send_text_response(500, f"▲ kubectl failed:\n{error_msg}")
        except subprocess.TimeoutExpired:
            self.send_text_response(504, "▲ Command timeout expired while connecting to cluster.")
        except Exception as e:
            self.send_text_response(500, f"▲ Server error executing kubectl logs: {str(e)}")

    def handle_pod_describe(self, query_string):
        params = parse_qs(query_string)
        pod = params.get('pod', [None])[0]
        namespace = params.get('namespace', [None])[0]

        if not pod or not namespace:
            self.send_error_json(400, "Missing required query parameters: 'pod' and 'namespace'")
            return

        if not re.match(r"^[a-z0-9.-]+$", pod) or not re.match(r"^[a-z0-9.-]+$", namespace):
            self.send_error_json(400, "Invalid characters in pod or namespace parameter")
            return

        with active_context_lock:
            ctx = active_context

        if ctx == "demo":
            mock_describe = f"""Name:         {pod}
Namespace:    {namespace}
Priority:     0
Node:         node-alpha/192.168.1.100
Start Time:   Fri, 22 May 2026 10:00:00 -0400
Labels:       app=demo-app
              pod-template-hash=7d4f9b8c
Annotations:  kubernetes.io/config.seen: 2026-05-22T10:00:00Z
Status:       Running
IP:           10.244.1.42
Containers:
  demo-container:
    Container ID:   containerd://abc123xyz
    Image:          nginx:stable-alpine
    Port:           80/TCP
    State:          Running
      Started:      Fri, 22 May 2026 10:00:02 -0400
    Ready:          True
    Restart Count:  0
    Requests:
      cpu:        100m
      memory:     12Gi
Conditions:
  Type              Status
  Initialized       True 
  Ready             True 
  ContainersReady   True 
  PodScheduled      True 
Events:
  Type    Reason     Age   From               Message
  ----    ------     ----  ----               -------
  Normal  Scheduled  15m   default-scheduler  Successfully assigned {namespace}/{pod} to node-alpha
  Normal  Pulling    15m   kubelet            Pulling image "nginx:stable-alpine"
  Normal  Pulled     14m   kubelet            Successfully pulled image "nginx:stable-alpine" in 2.3s
  Normal  Created    14m   kubelet            Created container demo-container
  Normal  Started    14m   kubelet            Started container demo-container"""
            self.send_text_response(200, mock_describe)
            return

        try:
            print(f"Running secure command: kubectl describe pod {pod} -n {namespace} (context: {ctx})")
            cmd = ["kubectl", "--context", ctx, "describe", "pod", pod, "-n", namespace]
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            if res.returncode == 0:
                self.send_text_response(200, res.stdout)
            else:
                error_msg = res.stderr or "Unknown error describing pod"
                self.send_text_response(500, f"▲ kubectl failed:\n{error_msg}")
        except subprocess.TimeoutExpired:
            self.send_text_response(504, "▲ Command timeout expired while connecting to cluster.")
        except Exception as e:
            self.send_text_response(500, f"▲ Server error executing kubectl describe: {str(e)}")

    def handle_pod_spec(self, query_string):
        params = parse_qs(query_string)
        pod = params.get('pod', [None])[0]
        namespace = params.get('namespace', [None])[0]

        if not pod or not namespace:
            self.send_error_json(400, "Missing required query parameters: 'pod' and 'namespace'")
            return

        if not re.match(r"^[a-z0-9.-]+$", pod) or not re.match(r"^[a-z0-9.-]+$", namespace):
            self.send_error_json(400, "Invalid characters in pod or namespace parameter")
            return

        with active_context_lock:
            ctx = active_context

        if ctx == "demo":
            try:
                state_file = parse_cluster.OUTPUT_FILE
                if os.path.exists(state_file):
                    with open(state_file, 'r', encoding='utf-8') as f:
                        state = json.load(f)
                    found_pod = None
                    for node in state.get("nodes", []):
                        for p in node.get("pods", []):
                            if p.get("name") == pod and p.get("namespace") == namespace:
                                found_pod = p.get("raw")
                                break
                    if found_pod:
                        def dict_to_yaml(d, indent=0):
                            lines = []
                            spacer = " " * indent
                            if isinstance(d, dict):
                                keys = list(d.keys())
                                preferred = ["apiVersion", "kind", "metadata", "spec", "status"]
                                sorted_keys = [k for k in preferred if k in keys] + [k for k in keys if k not in preferred]
                                for k in sorted_keys:
                                    v = d[k]
                                    if isinstance(v, (dict, list)):
                                        lines.append(f"{spacer}{k}:")
                                        lines.append(dict_to_yaml(v, indent + 2))
                                    else:
                                        lines.append(f"{spacer}{k}: {v}")
                            elif isinstance(d, list):
                                for item in d:
                                    if isinstance(item, (dict, list)):
                                        yaml_item = dict_to_yaml(item, indent + 2).lstrip()
                                        lines.append(f"{spacer}- {yaml_item}")
                                    else:
                                        lines.append(f"{spacer}- {item}")
                            else:
                                lines.append(f"{spacer}{d}")
                            return "\n".join(lines)
                        
                        yaml_text = dict_to_yaml(found_pod)
                        self.send_text_response(200, yaml_text)
                        return
            except Exception as e:
                print(f"Error serving mock pod spec: {e}")

            mock_spec = f"""apiVersion: v1
kind: Pod
metadata:
  name: {pod}
  namespace: {namespace}
  labels:
    app: demo-app
spec:
  containers:
  - name: main
    image: nginx:stable-alpine
    resources:
      requests:
        memory: 12Gi
        cpu: 500m"""
            self.send_text_response(200, mock_spec)
            return

        try:
            print(f"Running secure command: kubectl get pod {pod} -n {namespace} -o yaml (context: {ctx})")
            cmd = ["kubectl", "--context", ctx, "get", "pod", pod, "-n", namespace, "-o", "yaml"]
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            if res.returncode == 0:
                self.send_text_response(200, res.stdout)
            else:
                error_msg = res.stderr or "Unknown error fetching pod spec"
                self.send_text_response(500, f"▲ kubectl failed:\n{error_msg}")
        except subprocess.TimeoutExpired:
            self.send_text_response(504, "▲ Command timeout expired while connecting to cluster.")
        except Exception as e:
            self.send_text_response(500, f"▲ Server error executing kubectl get pod: {str(e)}")

    def handle_node_spec(self, query_string):
        params = parse_qs(query_string)
        node_name = params.get('node', [None])[0]

        if not node_name:
            self.send_error_json(400, "Missing required query parameter: 'node'")
            return

        if not re.match(r"^[a-z0-9.-]+$", node_name):
            self.send_error_json(400, "Invalid characters in node parameter")
            return

        with active_context_lock:
            ctx = active_context

        if ctx == "demo":
            try:
                state_file = parse_cluster.OUTPUT_FILE
                if os.path.exists(state_file):
                    with open(state_file, 'r', encoding='utf-8') as f:
                        state = json.load(f)
                    found_node = None
                    for n in state.get("nodes", []):
                        if n.get("name") == node_name:
                            found_node = n.get("raw")
                            break
                    if found_node:
                        def dict_to_yaml(d, indent=0):
                            lines = []
                            spacer = " " * indent
                            if isinstance(d, dict):
                                keys = list(d.keys())
                                preferred = ["apiVersion", "kind", "metadata", "spec", "status"]
                                sorted_keys = [k for k in preferred if k in keys] + [k for k in keys if k not in preferred]
                                for k in sorted_keys:
                                    v = d[k]
                                    if isinstance(v, (dict, list)):
                                        lines.append(f"{spacer}{k}:")
                                        lines.append(dict_to_yaml(v, indent + 2))
                                    else:
                                        lines.append(f"{spacer}{k}: {v}")
                            elif isinstance(d, list):
                                for item in d:
                                    if isinstance(item, (dict, list)):
                                        yaml_item = dict_to_yaml(item, indent + 2).lstrip()
                                        lines.append(f"{spacer}- {yaml_item}")
                                    else:
                                        lines.append(f"{spacer}- {item}")
                            else:
                                lines.append(f"{spacer}{d}")
                            return "\n".join(lines)
                        
                        yaml_text = dict_to_yaml(found_node)
                        self.send_text_response(200, yaml_text)
                        return
            except Exception as e:
                print(f"Error serving mock node spec: {e}")

            mock_spec = f"""apiVersion: v1
kind: Node
metadata:
  name: {node_name}
  labels:
    kubernetes.io/hostname: {node_name}
    kubernetes.io/os: linux
spec:
  providerID: aws:///us-east-1a/i-001c08c4cd51462ec
status:
  capacity:
    cpu: "8"
    memory: 24Gi
  allocatable:
    cpu: "8"
    memory: 24Gi"""
            self.send_text_response(200, mock_spec)
            return

        try:
            print(f"Running secure command: kubectl get node {node_name} -o yaml (context: {ctx})")
            cmd = ["kubectl", "--context", ctx, "get", "node", node_name, "-o", "yaml"]
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            if res.returncode == 0:
                self.send_text_response(200, res.stdout)
            else:
                error_msg = res.stderr or "Unknown error fetching node spec"
                self.send_text_response(500, f"▲ kubectl failed:\n{error_msg}")
        except subprocess.TimeoutExpired:
            self.send_text_response(504, "▲ Command timeout expired while connecting to cluster.")
        except Exception as e:
            self.send_text_response(500, f"▲ Server error executing kubectl get node: {str(e)}")

    def handle_get_events(self, query_string):
        params = parse_qs(query_string)
        name = params.get('name', [None])[0]
        kind = params.get('kind', [None])[0]
        namespace = params.get('namespace', [None])[0]

        if not name or not kind:
            self.send_error_json(400, "Missing required query parameters: 'name' and 'kind'")
            return

        if not re.match(r"^[a-z0-9.-]+$", name) or (namespace and not re.match(r"^[a-z0-9.-]+$", namespace)):
            self.send_error_json(400, "Invalid characters in name or namespace parameter")
            return

        kind = kind.lower()
        if kind not in ['pod', 'node']:
            self.send_error_json(400, "Invalid kind. Must be 'pod' or 'node'")
            return

        with active_context_lock:
            ctx = active_context

        if ctx == "demo":
            mock_events = f"""LAST SEEN   TYPE     REASON      OBJECT    MESSAGE
12m         Normal   Scheduled   pod/{name}   Successfully assigned {namespace or 'default'}/{name} to node
12m         Normal   Pulling     pod/{name}   Pulling image "nginx:stable-alpine"
11m         Normal   Pulled      pod/{name}   Successfully pulled image "nginx:stable-alpine" in 1.4s
11m         Normal   Created     pod/{name}   Created container
11m         Normal   Started     pod/{name}   Started container"""
            self.send_text_response(200, mock_events)
            return

        try:
            cmd = ["kubectl"]
            cmd += ["--context", ctx]
            if kind == 'pod':
                ns = namespace if namespace else 'default'
                cmd += ["get", "events", "-n", ns, "--field-selector", f"involvedObject.name={name}"]
            else: # node
                cmd += ["get", "events", "--all-namespaces", "--field-selector", f"involvedObject.name={name}"]

            print(f"Running secure command: {' '.join(cmd)}")
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            if res.returncode == 0:
                self.send_text_response(200, res.stdout or "No events found for this object.")
            else:
                error_msg = res.stderr or "Unknown error fetching events"
                self.send_text_response(500, f"▲ kubectl failed:\n{error_msg}")
        except subprocess.TimeoutExpired:
            self.send_text_response(504, "▲ Command timeout expired while connecting to cluster.")
        except Exception as e:
            self.send_text_response(500, f"▲ Server error executing kubectl events: {str(e)}")

    def send_json_response(self, code, data):
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        payload = json.dumps(data).encode('utf-8')
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def send_text_response(self, code, text):
        self.send_response(code)
        self.send_header('Content-Type', 'text/plain; charset=utf-8')
        payload = text.encode('utf-8')
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def send_error_json(self, code, message):
        self.send_json_response(code, {"error": message})

def run_bg_sync():
    """Background polling thread worker."""
    print("Background Kubernetes context poller started (30s interval).")
    while True:
        try:
            with active_context_lock:
                ctx = active_context
            if ctx != "demo":
                parse_cluster.parse_cluster(context=ctx)
        except SystemExit:
            print(f"Background sync failed for context '{ctx}' (SystemExit)")
        except Exception as e:
            print(f"Error in background sync worker: {e}")
        time.sleep(30)

def init_active_context():
    """Attempts to discover active kubectl context at boot."""
    global active_context
    try:
        res = subprocess.run(["kubectl", "config", "current-context"], capture_output=True, text=True, timeout=2)
        if res.returncode == 0 and res.stdout.strip():
            ctx = res.stdout.strip()
            with active_context_lock:
                active_context = ctx
            print(f"Discovered active kubectl context on boot: {ctx}")
        else:
            with active_context_lock:
                active_context = "demo"
            print("No active kubectl context found on boot. Defaulting to 'demo' context.")
    except Exception as e:
        with active_context_lock:
            active_context = "demo"
        print(f"Failed to query active context on boot: {e}. Defaulting to 'demo' context.")

def main():
    global active_context
    # Discover active context dynamically on startup
    init_active_context()

    with active_context_lock:
        ctx = active_context

    print(f"Bootstrapping cluster state for context: {ctx}...")
    try:
        if ctx == "demo":
            parse_cluster.parse_cluster(force_mock=True)
        else:
            parse_cluster.parse_cluster(context=ctx)
    except SystemExit:
        print(f"Warning: Context '{ctx}' is unreachable on boot. Falling back to Demo Mode...")
        parse_cluster.parse_cluster(force_mock=True)
        with active_context_lock:
            active_context = "demo"
    except Exception as e:
        print(f"Initial context parsing completed with warning: {e}. Falling back to Demo Mode...")
        parse_cluster.parse_cluster(force_mock=True)
        with active_context_lock:
            active_context = "demo"

    # Launch background thread
    t = threading.Thread(target=run_bg_sync, daemon=True)
    t.start()

    # Serve HTTP
    server_address = (BIND_ADDRESS, PORT)
    httpd = http.server.HTTPServer(server_address, LocalAPIServer)
    print(f"============================================================")
    print(f"🚀 Lex Server running successfully!")
    print(f"🔗 Local Web Interface: http://{BIND_ADDRESS}:{PORT}/")
    print(f"============================================================")
    
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down server...")
        httpd.server_close()

if __name__ == '__main__':
    main()
