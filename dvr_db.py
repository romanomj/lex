#!/usr/bin/env python3
"""
SQLite Database interface for Lex metaphorical DVR Mode.
Zero external dependencies, robust, and thread-safe operations.
"""

import os
import sqlite3
import json
import zlib
import datetime
import re
import alerts
from parse_cluster import DATA_DIR

DB_FILE = os.path.join(DATA_DIR, "lex_dvr.db")

def get_db_connection():
    """Returns a connection to the SQLite database with row factory enabled."""
    os.makedirs(os.path.dirname(DB_FILE) or ".", exist_ok=True)
    conn = sqlite3.connect(DB_FILE, timeout=10.0)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    """Initializes schemas and indexes for recording_sessions and snapshots tables."""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        
        # 1. Create recording sessions table
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS recording_sessions (
            session_id TEXT PRIMARY KEY,
            cluster_name TEXT NOT NULL,
            start_time TEXT NOT NULL,
            end_time TEXT,
            snapshot_count INTEGER DEFAULT 0,
            is_active INTEGER DEFAULT 1
        )
        """)
        
        # 2. Create snapshots table
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            cluster_name TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            state_json TEXT NOT NULL,
            FOREIGN KEY (session_id) REFERENCES recording_sessions (session_id) ON DELETE CASCADE
        )
        """)
        
        # Migrations: compressed state + precomputed timeline summary (older rows keep using state_json)
        existing_cols = {row[1] for row in cursor.execute("PRAGMA table_info(snapshots)")}
        for col, ddl in (("state_blob", "BLOB"), ("summary_json", "TEXT"), ("has_incident", "INTEGER DEFAULT 0")):
            if col not in existing_cols:
                cursor.execute(f"ALTER TABLE snapshots ADD COLUMN {col} {ddl}")

        # Vital-signs history (metrics.py): one small summary row per scrape and context
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS metrics_points (
            context TEXT NOT NULL,
            ts TEXT NOT NULL,
            data TEXT NOT NULL
        )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_metrics_context_ts ON metrics_points(context, ts)")

        # Indexes for fast querying
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_snapshots_session ON snapshots(session_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_snapshots_timestamp ON snapshots(timestamp)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_sessions_cluster ON recording_sessions(cluster_name)")
        
        # Enable WAL mode for high concurrency
        cursor.execute("PRAGMA journal_mode=WAL")
        
        conn.commit()
        for suffix in ("", "-wal", "-shm"):   # owner-only, like the rest of the data directory
            if os.path.exists(DB_FILE + suffix):
                os.chmod(DB_FILE + suffix, 0o600)
        print(f"✔ SQLite database '{DB_FILE}' successfully initialized.")
    except Exception as e:
        print(f"▲ Error initializing database: {e}")
        conn.rollback()
    finally:
        conn.close()

def start_session(cluster_name):
    """
    Initializes a new recording session. 
    Generates a human-readable session_id using datetime.
    """
    now = datetime.datetime.now(datetime.timezone.utc)
    timestamp_str = now.strftime("%Y-%m-%d_%H%M%S")
    safe_cluster = re.sub(r"[^a-zA-Z0-9_-]", "-", cluster_name)
    session_id = f"session_{safe_cluster}_{timestamp_str}"
    start_time_iso = now.isoformat()
    
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        
        # Deactivate any currently active recording sessions for this specific cluster context
        cursor.execute("""
        UPDATE recording_sessions 
        SET is_active = 0, end_time = ?
        WHERE cluster_name = ? AND is_active = 1
        """, (start_time_iso, cluster_name))
        
        # Insert new session
        cursor.execute("""
        INSERT INTO recording_sessions (session_id, cluster_name, start_time, is_active, snapshot_count)
        VALUES (?, ?, ?, 1, 0)
        """, (session_id, cluster_name, start_time_iso))
        
        conn.commit()
        print(f"● Started new recording session '{session_id}' for cluster '{cluster_name}'")
        return session_id
    except Exception as e:
        print(f"▲ Error starting session: {e}")
        conn.rollback()
        return None
    finally:
        conn.close()

def summarize_state(state_dict, now=None):
    """
    Small per-frame summary used to draw the DVR timeline without downloading every frame.
    Incidents come from the alert engine (the same rules the live UI uses).
    """
    now = now or datetime.datetime.now(datetime.timezone.utc)
    frame_alerts = state_dict.get("alerts")
    if frame_alerts is None:
        # Frames recorded before alerts existed: evaluate statelessly
        frame_alerts = alerts.evaluate(state_dict, now=now, tracker=alerts.AlertTracker())
    nodes = state_dict.get("nodes") or []
    problem_nodes, problem_pods = [], []
    for a in frame_alerts:
        subject = a.get("subject") or {}
        if subject.get("kind") == "node" and subject.get("name") not in problem_nodes:
            problem_nodes.append(subject.get("name"))
        elif subject.get("kind") == "pod":
            key = f"{subject.get('namespace')}/{subject.get('name')}"
            if key not in problem_pods:
                problem_pods.append(key)
    return {
        "nodes": len(nodes),
        "pods": sum(len(n.get("pods") or []) for n in nodes),
        "unscheduledPods": len(state_dict.get("unscheduledPods") or []),
        "criticalAlerts": sum(1 for a in frame_alerts if a.get("severity") == "critical"),
        "warningAlerts": sum(1 for a in frame_alerts if a.get("severity") == "warning"),
        "problemNodes": problem_nodes[:20],
        "problemPods": problem_pods[:20],
        "problemNodeCount": len(problem_nodes),
        "problemPodCount": len(problem_pods),
    }

def add_snapshot(session_id, cluster_name, state_dict):
    """
    Adds a cluster state snapshot to an active session (zlib-compressed, with a timeline summary).
    Automatically increments the session snapshot_count.
    """
    now = datetime.datetime.now(datetime.timezone.utc)
    timestamp_iso = now.isoformat()
    state_blob = zlib.compress(json.dumps(state_dict, separators=(",", ":")).encode("utf-8"), 6)
    summary = summarize_state(state_dict, now)
    has_incident = 1 if (summary["problemNodeCount"] or summary["problemPodCount"]) else 0

    conn = get_db_connection()
    try:
        cursor = conn.cursor()

        # Verify session exists
        cursor.execute("SELECT session_id FROM recording_sessions WHERE session_id = ?", (session_id,))
        if not cursor.fetchone():
            # Session might have been deleted, ignore
            return False

        cursor.execute("""
        INSERT INTO snapshots (session_id, cluster_name, timestamp, state_json, state_blob, summary_json, has_incident)
        VALUES (?, ?, ?, '', ?, ?, ?)
        """, (session_id, cluster_name, timestamp_iso, state_blob, json.dumps(summary), has_incident))

        # Update snapshot count in session
        cursor.execute("""
        UPDATE recording_sessions
        SET snapshot_count = snapshot_count + 1
        WHERE session_id = ?
        """, (session_id,))

        conn.commit()
        return True
    except Exception as e:
        print(f"▲ Error saving snapshot to session '{session_id}': {e}")
        conn.rollback()
        return False
    finally:
        conn.close()

def end_session(session_id):
    """Concludes a recording session by setting it inactive and recording the end time."""
    now = datetime.datetime.now(datetime.timezone.utc)
    end_time_iso = now.isoformat()
    
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
        UPDATE recording_sessions 
        SET is_active = 0, end_time = ?
        WHERE session_id = ?
        """, (end_time_iso, session_id))
        conn.commit()
        print(f"■ Concluded recording session '{session_id}'")
        return True
    except Exception as e:
        print(f"▲ Error ending session '{session_id}': {e}")
        conn.rollback()
        return False
    finally:
        conn.close()

def get_active_session(cluster_name):
    """Retrieves active session details for a specific cluster (if recording is active)."""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
        SELECT session_id, cluster_name, start_time, snapshot_count 
        FROM recording_sessions 
        WHERE cluster_name = ? AND is_active = 1
        LIMIT 1
        """, (cluster_name,))
        row = cursor.fetchone()
        if row:
            return dict(row)
        return None
    except Exception as e:
        print(f"▲ Error getting active session: {e}")
        return None
    finally:
        conn.close()

def get_sessions():
    """Retrieves all sessions from the database ordered by start_time descending."""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
        SELECT session_id, cluster_name, start_time, end_time, snapshot_count, is_active 
        FROM recording_sessions 
        ORDER BY start_time DESC
        """)
        rows = cursor.fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        print(f"▲ Error listing sessions: {e}")
        return []
    finally:
        conn.close()

def _decode_state(row):
    if row["state_blob"]:
        return json.loads(zlib.decompress(row["state_blob"]).decode("utf-8"))
    return json.loads(row["state_json"])  # rows written before compression was added

def get_timeline(session_id):
    """Chronological frame list for a session: ids, timestamps and incident summaries, without state."""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
        SELECT id, timestamp, has_incident, summary_json, state_blob, state_json
        FROM snapshots
        WHERE session_id = ?
        ORDER BY timestamp ASC
        """, (session_id,))
        frames = []
        for r in cursor.fetchall():
            summary = json.loads(r["summary_json"]) if r["summary_json"] else None
            has_incident = bool(r["has_incident"])
            if summary is None:
                # Legacy row: compute the summary once from the stored state
                try:
                    summary = summarize_state(_decode_state(r))
                    has_incident = bool(summary["problemNodeCount"] or summary["problemPodCount"])
                except Exception:
                    summary = {}
            frames.append({"id": r["id"], "timestamp": r["timestamp"], "hasIncident": has_incident, "summary": summary})
        return frames
    except Exception as e:
        print(f"▲ Error retrieving timeline for '{session_id}': {e}")
        return []
    finally:
        conn.close()

def get_snapshot(session_id, snapshot_id):
    """A single frame's full state, or None."""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
        SELECT id, timestamp, state_blob, state_json FROM snapshots WHERE session_id = ? AND id = ?
        """, (session_id, snapshot_id))
        row = cursor.fetchone()
        if not row:
            return None
        return {"id": row["id"], "timestamp": row["timestamp"], "state": _decode_state(row)}
    except Exception as e:
        print(f"▲ Error retrieving snapshot {snapshot_id} for '{session_id}': {e}")
        return None
    finally:
        conn.close()

def prune_old_snapshots(max_age_days):
    """Retention: deletes frames older than max_age_days and finished sessions left with no frames."""
    if not max_age_days or max_age_days <= 0:
        return 0
    cutoff = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=max_age_days)).isoformat()
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM snapshots WHERE timestamp < ?", (cutoff,))
        removed = cursor.rowcount
        cursor.execute("""
        UPDATE recording_sessions
        SET snapshot_count = (SELECT COUNT(*) FROM snapshots s WHERE s.session_id = recording_sessions.session_id)
        """)
        cursor.execute("DELETE FROM recording_sessions WHERE is_active = 0 AND snapshot_count = 0")
        conn.commit()
        if removed:
            print(f"✔ DVR retention: removed {removed} snapshots older than {max_age_days} days.")
        return removed
    except Exception as e:
        print(f"▲ Error pruning snapshots: {e}")
        conn.rollback()
        return 0
    finally:
        conn.close()

def delete_session(session_id):
    """Deletes a recording session and cascades deletion to all stored snapshots."""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        
        # Cascading deletion (SQLite requires foreign keys or we do it manually)
        cursor.execute("DELETE FROM snapshots WHERE session_id = ?", (session_id,))
        cursor.execute("DELETE FROM recording_sessions WHERE session_id = ?", (session_id,))
        
        conn.commit()
        print(f"✔ Successfully deleted session '{session_id}' and all snapshots.")
        return True
    except Exception as e:
        print(f"▲ Error deleting session '{session_id}': {e}")
        conn.rollback()
        return False
    finally:
        conn.close()
