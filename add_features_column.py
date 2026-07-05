"""
One-time migration: adds the 'features' column to the room table.
Run this once after pulling the amenities-categories update:
    python add_features_column.py
"""
import sqlite3

conn = sqlite3.connect('riganest.db')
try:
    conn.execute("ALTER TABLE room ADD COLUMN features TEXT DEFAULT '{}'")
    conn.commit()
    print("Column 'features' added successfully!")
except sqlite3.OperationalError as e:
    print("Note:", e)
conn.close()
