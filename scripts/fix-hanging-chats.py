#!/usr/bin/env python3
"""Mark hanging assistant messages (done=0) as done so chats unstick."""
import sqlite3, json, time

DB = '/home/ankit/hroot/devserver/volumes/open-webui/webui.db'

conn = sqlite3.connect(DB)
rows = conn.execute("""
    SELECT cm.id, cm.chat_id, COALESCE(c.title, ''), cm.created_at
    FROM chat_message cm
    JOIN chat c ON c.id = cm.chat_id
    WHERE cm.role = 'assistant' AND cm.done = 0
    ORDER BY cm.created_at DESC
""").fetchall()

if not rows:
    print("No hanging messages found")
else:
    print(f"Found {len(rows)} hanging message(s):")
    for r in rows:
        age_mins = (int(time.time()) - r[3]) // 60
        print(f"  {r[0][:20]}... | \"{r[2][:50]}\" | age: {age_mins}m")
        conn.execute("UPDATE chat_message SET done = 1, updated_at = ? WHERE id = ?",
                     (int(time.time()), r[0]))
        conn.execute("UPDATE chat SET updated_at = ? WHERE id = ?",
                     (int(time.time()), r[1]))
    conn.commit()
    print(f"Fixed {len(rows)} hanging message(s)")

conn.close()
