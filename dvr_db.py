#!/usr/bin/env python3
"""
SQLite Database interface for Lex metaphorical DVR Mode.
Zero external dependencies, robust, and thread-safe operations.
"""

import os
import sqlite3
import json
import datetime

DB_FILE = "lex_dvr.db"

def get_db_connection():
    """Returns a connection to the SQLite database with row factory enabled."""
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
        
        # Indexes for fast querying
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_snapshots_session ON snapshots(session_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_snapshots_timestamp ON snapshots(timestamp)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_sessions_cluster ON recording_sessions(cluster_name)")
        
        # Enable WAL mode for high concurrency
        cursor.execute("PRAGMA journal_mode=WAL")
        
        conn.commit()
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
    session_id = f"session_{cluster_name}_{timestamp_str}"
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

def add_snapshot(session_id, cluster_name, state_dict):
    """
    Adds a cluster state snapshot to an active session.
    Automatically increments the session snapshot_count.
    """
    now = datetime.datetime.now(datetime.timezone.utc)
    timestamp_iso = now.isoformat()
    state_json = json.dumps(state_dict)
    
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        
        # Verify session exists and is active
        cursor.execute("SELECT session_id FROM recording_sessions WHERE session_id = ?", (session_id,))
        if not cursor.fetchone():
            # Session might have been deleted, ignore
            return False
            
        # Insert snapshot
        cursor.execute("""
        INSERT INTO snapshots (session_id, cluster_name, timestamp, state_json)
        VALUES (?, ?, ?, ?)
        """, (session_id, cluster_name, timestamp_iso, state_json))
        
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

def get_snapshots(session_id):
    """Retrieves all chronological snapshots for a session."""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
        SELECT id, session_id, cluster_name, timestamp, state_json 
        FROM snapshots 
        WHERE session_id = ?
        ORDER BY timestamp ASC
        """, (session_id,))
        rows = cursor.fetchall()
        
        snapshots = []
        for r in rows:
            snap = dict(r)
            # Parse state_json back into dict so API can send it clean
            try:
                snap["state"] = json.loads(snap["state_json"])
                del snap["state_json"] # Free memory/clean payload
            except Exception:
                snap["state"] = None
            snapshots.append(snap)
        return snapshots
    except Exception as e:
        print(f"▲ Error retrieving snapshots for '{session_id}': {e}")
        return []
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
