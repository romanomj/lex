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
        # Route: API logs fetch
        elif path == '/api/v1/pods/logs':
            self.handle_pod_logs(parsed_url.query)
        # Route: API pod describe
        elif path == '/api/v1/pods/describe':
            self.handle_pod_describe(parsed_url.query)
        # Route: API pod spec
        elif path == '/api/v1/pods/spec':
            self.handle_pod_spec(parsed_url.query)
        # Route: API events fetch
        elif path == '/api/v1/events':
            self.handle_get_events(parsed_url.query)
        else:
            # Fallback to serving static files (lex.html, etc.) from the workspace directory
            super().do_GET()

    def handle_get_state(self):
        state_file = parse_cluster.OUTPUT_FILE
        if not os.path.exists(state_file):
            # If cluster_state.json doesn't exist yet, force run parsing once
            print("State file missing, generating synchronously...")
            try:
                parse_cluster.parse_cluster()
            except Exception as e:
                self.send_error_json(500, f"Error generating cluster state: {str(e)}")
                return

        if os.path.exists(state_file):
            try:
                with open(state_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                self.send_json_response(200, data)
            except Exception as e:
                self.send_error_json(500, f"Error reading state file: {str(e)}")
        else:
            self.send_error_json(404, "Cluster state file could not be found.")

    def handle_pod_logs(self, query_string):
        params = parse_qs(query_string)
        pod = params.get('pod', [None])[0]
        namespace = params.get('namespace', [None])[0]

        if not pod or not namespace:
            self.send_error_json(400, "Missing required query parameters: 'pod' and 'namespace'")
            return

        # Secure parameters: strictly match Kubernetes DNS label / namespace rules
        if not re.match(r"^[a-z0-9.-]+$", pod) or not re.match(r"^[a-z0-9.-]+$", namespace):
            self.send_error_json(400, "Invalid characters in pod or namespace parameter")
            return

        # Execute read-only command securely using argument lists (no shell=True)
        try:
            print(f"Running secure command: kubectl logs {pod} -n {namespace} --tail=200")
            res = subprocess.run(
                ["kubectl", "logs", pod, "-n", namespace, "--tail=200"],
                capture_output=True,
                text=True,
                timeout=10
            )
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

        # Secure parameters: strictly match Kubernetes DNS label rules
        if not re.match(r"^[a-z0-9.-]+$", pod) or not re.match(r"^[a-z0-9.-]+$", namespace):
            self.send_error_json(400, "Invalid characters in pod or namespace parameter")
            return

        # Execute describe command securely using argument lists (no shell=True)
        try:
            print(f"Running secure command: kubectl describe pod {pod} -n {namespace}")
            res = subprocess.run(
                ["kubectl", "describe", "pod", pod, "-n", namespace],
                capture_output=True,
                text=True,
                timeout=10
            )
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

        # Secure parameters: strictly match Kubernetes DNS label rules
        if not re.match(r"^[a-z0-9.-]+$", pod) or not re.match(r"^[a-z0-9.-]+$", namespace):
            self.send_error_json(400, "Invalid characters in pod or namespace parameter")
            return

        # Execute get pod -o yaml command securely using argument lists (no shell=True)
        try:
            print(f"Running secure command: kubectl get pod {pod} -n {namespace} -o yaml")
            res = subprocess.run(
                ["kubectl", "get", "pod", pod, "-n", namespace, "-o", "yaml"],
                capture_output=True,
                text=True,
                timeout=10
            )
            if res.returncode == 0:
                self.send_text_response(200, res.stdout)
            else:
                error_msg = res.stderr or "Unknown error fetching pod spec"
                self.send_text_response(500, f"▲ kubectl failed:\n{error_msg}")
        except subprocess.TimeoutExpired:
            self.send_text_response(504, "▲ Command timeout expired while connecting to cluster.")
        except Exception as e:
            self.send_text_response(500, f"▲ Server error executing kubectl get pod: {str(e)}")

    def handle_get_events(self, query_string):
        params = parse_qs(query_string)
        name = params.get('name', [None])[0]
        kind = params.get('kind', [None])[0]
        namespace = params.get('namespace', [None])[0]

        if not name or not kind:
            self.send_error_json(400, "Missing required query parameters: 'name' and 'kind'")
            return

        # Secure parameters: strictly match Kubernetes DNS label / namespace rules
        if not re.match(r"^[a-z0-9.-]+$", name) or (namespace and not re.match(r"^[a-z0-9.-]+$", namespace)):
            self.send_error_json(400, "Invalid characters in name or namespace parameter")
            return

        kind = kind.lower()
        if kind not in ['pod', 'node']:
            self.send_error_json(400, "Invalid kind. Must be 'pod' or 'node'")
            return

        # Execute read-only command securely using argument lists (no shell=True)
        try:
            if kind == 'pod':
                ns = namespace if namespace else 'default'
                cmd = ["kubectl", "get", "events", "-n", ns, "--field-selector", f"involvedObject.name={name}"]
            else: # node
                cmd = ["kubectl", "get", "events", "--all-namespaces", "--field-selector", f"involvedObject.name={name}"]

            print(f"Running secure command: {' '.join(cmd)}")
            res = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=10
            )
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
            parse_cluster.parse_cluster()
        except Exception as e:
            print(f"Error in background sync worker: {e}")
        time.sleep(30)

def main():
    # Force single parse run on boot to establish cluster context early
    print("Bootstrapping cluster state...")
    try:
        parse_cluster.parse_cluster()
    except Exception as e:
        print(f"Initial context parsing completed with warning: {e}")

    # Launch background thread
    t = threading.Thread(target=run_bg_sync, daemon=True)
    t.start()

    # Serve HTTP
    server_address = (BIND_ADDRESS, PORT)
    httpd = http.server.HTTPServer(server_address, LocalAPIServer)
    print(f"============================================================")
    print(f"🚀 Lex Server running successfully!")
    print(f"🔗 Local Web Interface: http://{BIND_ADDRESS}:{PORT}/lex.html")
    print(f"============================================================")
    
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down server...")
        httpd.server_close()

if __name__ == '__main__':
    main()
