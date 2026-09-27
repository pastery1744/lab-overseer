import sqlite3, threading, time, random, string

SCHEMA = """
CREATE TABLE IF NOT EXISTS incidents(
  id TEXT PRIMARY KEY, fingerprint TEXT, target TEXT, tier INT, summary TEXT,
  action TEXT, action_arg TEXT, mode TEXT, status TEXT,
  created REAL, execute_after REAL, updated REAL, result TEXT);
CREATE TABLE IF NOT EXISTS actions_log(ts REAL, incident TEXT, target TEXT, action TEXT, arg TEXT, dry_run INT, ok INT, detail TEXT);
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
"""
OPEN = ("open", "scheduled", "pending_approval")


class State:
    def __init__(self, path):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        with self.lock:
            self.db.executescript(SCHEMA)

    def _q(self, sql, args=()):
        with self.lock:
            cur = self.db.execute(sql, args)
            self.db.commit()
            return [dict(r) for r in cur.fetchall()]

    def new_id(self):
        while True:
            i = random.choice(string.ascii_uppercase) + "".join(random.choices(string.digits, k=2))
            if not self._q("SELECT 1 FROM incidents WHERE id=?", (i,)):
                return i

    def open_by_fingerprint(self, fp):
        r = self._q(f"SELECT * FROM incidents WHERE fingerprint=? AND status IN {OPEN}", (fp,))
        return r[0] if r else None

    def open_incidents(self):
        return self._q(f"SELECT * FROM incidents WHERE status IN {OPEN}")

    def recent(self, window=86400):
        return self._q("SELECT * FROM incidents WHERE created>? ORDER BY created", (time.time() - window,))

    def get(self, iid):
        r = self._q("SELECT * FROM incidents WHERE id=?", (iid.upper(),))
        return r[0] if r else None

    def create(self, fp, target, tier, summary, action, arg, mode, status, execute_after=None):
        iid, now = self.new_id(), time.time()
        self._q("INSERT INTO incidents VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (iid, fp, target, tier, summary, action, arg, mode, status, now, execute_after, now, ""))
        return iid

    def set_status(self, iid, status, result=""):
        self._q("UPDATE incidents SET status=?, result=?, updated=? WHERE id=?", (status, result, time.time(), iid))

    def due(self):
        return self._q("SELECT * FROM incidents WHERE status='scheduled' AND execute_after<=?", (time.time(),))

    def log_action(self, iid, target, action, arg, dry, ok, detail):
        self._q("INSERT INTO actions_log VALUES(?,?,?,?,?,?,?,?)", (time.time(), iid, target, action, arg, int(dry), int(ok), detail))

    def recent_actions(self, target, window=3600):
        return self._q("SELECT COUNT(*) c FROM actions_log WHERE target=? AND ts>?", (target, time.time() - window))[0]["c"]

    def prune(self, days=90):
        """Drop closed incidents and action logs older than `days` so the DB doesn't grow forever."""
        cut = time.time() - days * 86400
        self._q(f"DELETE FROM incidents WHERE updated<? AND status NOT IN {OPEN}", (cut,))
        self._q("DELETE FROM actions_log WHERE ts<?", (cut,))

    def meta_get(self, k, default=None):
        r = self._q("SELECT v FROM meta WHERE k=?", (k,))
        return r[0]["v"] if r else default

    def meta_set(self, k, v):
        self._q("INSERT OR REPLACE INTO meta VALUES(?,?)", (k, str(v)))
