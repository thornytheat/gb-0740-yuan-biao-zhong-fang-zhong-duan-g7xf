import json
import sqlite3
from contextlib import closing
from pathlib import Path


class Catalog:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connection()) as db, db:
            db.execute("CREATE TABLE IF NOT EXISTS plans (id TEXT PRIMARY KEY, document TEXT NOT NULL)")
            db.execute(
                "CREATE TABLE IF NOT EXISTS credentials "
                "(id TEXT PRIMARY KEY, plan TEXT NOT NULL, document TEXT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS evidence (id TEXT PRIMARY KEY, document TEXT NOT NULL)"
            )

    def connection(self):
        return sqlite3.connect(self.path, timeout=10)

    def get(self, key):
        with closing(self.connection()) as db:
            row = db.execute("SELECT document FROM plans WHERE id=?", (key,)).fetchone()
            return json.loads(row[0]) if row else None

    def save(self, key, result):
        with closing(self.connection()) as db, db:
            op = db.execute(
                "INSERT OR IGNORE INTO plans VALUES (?,?)", (key, json.dumps(result, ensure_ascii=False))
            )
            record = db.execute("SELECT document FROM plans WHERE id=?", (key,)).fetchone()[0]
            return json.loads(record), not op.rowcount

    def latest(self):
        with closing(self.connection()) as db:
            return [
                json.loads(row[0])["批次"]
                for row in db.execute("SELECT document FROM plans ORDER BY rowid DESC LIMIT 30")
            ]

    def get_credential(self, key):
        with closing(self.connection()) as db:
            row = db.execute("SELECT document FROM credentials WHERE id=?", (key,)).fetchone()
            return json.loads(row[0]) if row else None

    def save_credential(self, key, plan, result):
        with closing(self.connection()) as db, db:
            op = db.execute(
                "INSERT OR IGNORE INTO credentials VALUES (?,?,?)",
                (key, plan, json.dumps(result, ensure_ascii=False)),
            )
            record = db.execute(
                "SELECT document FROM credentials WHERE id=?", (key,)
            ).fetchone()[0]
            return json.loads(record), not op.rowcount

    def credentials_for_plan(self, plan):
        with closing(self.connection()) as db:
            return [
                json.loads(row[0])
                for row in db.execute(
                    "SELECT document FROM credentials WHERE plan=? ORDER BY rowid DESC LIMIT 30",
                    (plan,),
                )
            ]

    def save_evidence(self, key, document):
        with closing(self.connection()) as db, db:
            db.execute(
                "INSERT OR IGNORE INTO evidence VALUES (?,?)",
                (key, json.dumps(document, ensure_ascii=False)),
            )

    def get_evidence(self, key):
        with closing(self.connection()) as db:
            row = db.execute("SELECT document FROM evidence WHERE id=?", (key,)).fetchone()
            return json.loads(row[0]) if row else None

    def drop_evidence(self, key):
        with closing(self.connection()) as db, db:
            op = db.execute("DELETE FROM evidence WHERE id=?", (key,))
            return op.rowcount > 0
