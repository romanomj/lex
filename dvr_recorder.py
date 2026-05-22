#!/usr/bin/env python3
"""
Lex External Daemon Recorder.
Periodically queries active Kubernetes contexts, compiles visual states,
and records snapshots straight into the SQLite database.
Supports recording multiple contexts concurrently.
"""

import os
import sys
import time
import argparse
import signal
import dvr_db
import parse_cluster

active_sessions = {}

def graceful_shutdown(signum, frame):
    """Gracefully ends all open sessions on termination signal."""
    print("\n■ Shutting down background recorder daemon...")
    for context, session_id in list(active_sessions.items()):
        try:
            dvr_db.end_session(session_id)
            print(f"✔ Finalized recording session '{session_id}' for context '{context}'.")
        except Exception as e:
            print(f"▲ Error ending session for '{context}': {e}")
    sys.exit(0)

def main():
    parser = argparse.ArgumentParser(description="Lex External Daemon Recorder")
    parser.add_argument(
        "--contexts", 
        type=str, 
        default="", 
        help="Comma-separated Kubernetes contexts to record (e.g., 'docker-desktop,minikube'). Defaults to live active context if empty."
    )
    parser.add_argument(
        "--interval", 
        type=int, 
        default=30, 
        help="Polling and recording interval in seconds (default: 30)."
    )
    parser.add_argument(
        "--mock",
        action="store_true",
        help="If set, uses force mock parsing (useful for local development without actual clusters)."
    )
    
    args = parser.parse_args()
    
    # Initialize DB schemas
    dvr_db.init_db()
    
    # Setup signal handlers for clean exit
    signal.signal(signal.SIGINT, graceful_shutdown)
    signal.signal(signal.SIGTERM, graceful_shutdown)
    
    # Resolve target contexts
    target_contexts = []
    if args.contexts:
        target_contexts = [ctx.strip() for ctx in args.contexts.split(",") if ctx.strip()]
    else:
        # Fallback to current context
        ctx = parse_cluster.get_cluster_name()
        if ctx:
            target_contexts = [ctx]
        else:
            target_contexts = ["demo"]
            
    print("============================================================")
    print("🎥 Lex External Recording Daemon Booted!")
    print(f"● Target Contexts: {', '.join(target_contexts)}")
    print(f"● Polling Interval: {args.interval} seconds")
    print(f"● Database Target: {os.path.abspath(dvr_db.DB_FILE)}")
    print("============================================================")
    
    # Start sessions
    for ctx in target_contexts:
        session_id = dvr_db.start_session(ctx)
        if session_id:
            active_sessions[ctx] = session_id
        else:
            print(f"▲ Warning: Failed to create session for context '{ctx}'. Skipped.")
            
    if not active_sessions:
        print("▲ Error: No active sessions could be established. Exiting.")
        sys.exit(1)
        
    print("\n● Background recording loops started. Press Ctrl+C to terminate and finalize.")
    
    tick = 0
    while True:
        tick += 1
        start_time = time.time()
        
        print(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] --- Polling Cycle #{tick} ---")
        for ctx, session_id in list(active_sessions.items()):
            print(f" ➜ Scrape: Context '{ctx}'...")
            try:
                # Compile visual state. Programmatic call with write_to_file=False to keep caches clean
                # unless it is the demo mode, in which case we force mock.
                force_mock_mode = args.mock or (ctx == "demo")
                
                # Fetch state in-memory
                state = parse_cluster.parse_cluster(
                    context=ctx, 
                    force_mock=force_mock_mode, 
                    write_to_file=False
                )
                
                # Add to DB snapshot list
                success = dvr_db.add_snapshot(session_id, ctx, state)
                if success:
                    print(f"   ✔ Snapshot stored! Session: '{session_id}' | Nodes: {len(state.get('nodes', []))}")
                else:
                    print(f"   ▲ Snapshot write failed. Session might have been cleared.")
            except SystemExit:
                print(f"   ▲ Scrape failed: Context '{ctx}' is currently unreachable.")
            except Exception as e:
                print(f"   ▲ Error processing context '{ctx}': {e}")
                
        # Calculate time spent and dynamic sleep to keep polling consistent
        elapsed = time.time() - start_time
        sleep_duration = max(0.1, args.interval - elapsed)
        time.sleep(sleep_duration)

if __name__ == "__main__":
    main()
