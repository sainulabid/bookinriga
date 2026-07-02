import sqlite3
conn = sqlite3.connect('riganest.db')
try:
    conn.execute("ALTER TABLE site_page ADD COLUMN image VARCHAR(255) DEFAULT ''")
    conn.commit()
    print("Column added successfully!")
except sqlite3.OperationalError as e:
    print("Note:", e)
conn.close()
