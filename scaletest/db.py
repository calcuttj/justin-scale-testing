"""Small SQLite database recording everything a suite has run.

One database file per suite: ``<state_dir>/<suite>.sqlite``. It is local
bookkeeping (gitignored), not shared state; the source of truth for job and
file states is still justIN itself.

Tables
------
scenario_versions
    One row per distinct resolved config of a scenario. Every command first
    "syncs" the YAML: if a scenario's resolved config hash (Scenario.config_sha)
    is new, a row with the next version number is added. Test runs and batches
    point at the version that was current when they ran, so editing the YAML
    later never rewrites history: old batches keep the settings they used.

blobs
    Content-addressed store (sha256 -> text) for jobscripts. A jobscript can
    change without the scenario config changing (it's a separate file), so
    each test run and batch records the sha of the exact jobscript text used.

test_runs
    One row per `scale-test test` (a justin-test-jobscript run). ``dev`` says
    whether local dev extras were used; only non-dev passes count as a
    "tested" scenario version when submitting.

batches
    One row per `scale-test submit`. Each batch gets its own justIN campaign
    (justin_campaign_id) holding its workflows.

workflows
    One row per justIN workflow created by a batch, with the last state seen
    by `scale-test status`.

Times are stored as ISO-8601 UTC strings. Commands, configs and check results
are stored as JSON text.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS scenario_versions (
    id          INTEGER PRIMARY KEY,
    suite       TEXT NOT NULL,
    name        TEXT NOT NULL,
    version     INTEGER NOT NULL,
    config_json TEXT NOT NULL,
    config_sha  TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    UNIQUE (suite, name, version),
    UNIQUE (suite, name, config_sha)
);
CREATE TABLE IF NOT EXISTS blobs (
    sha  TEXT PRIMARY KEY,
    text TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS test_runs (
    id                  INTEGER PRIMARY KEY,
    scenario_version_id INTEGER NOT NULL REFERENCES scenario_versions(id),
    dev                 INTEGER NOT NULL,
    started_at          TEXT NOT NULL,
    finished_at         TEXT,
    result              TEXT,          -- 'pass' | 'fail' | NULL while running
    result_detail       TEXT,          -- JSON list of check messages
    workdir             TEXT,          -- /tmp/justin-test-jobscript.XXXXXX
    log_path            TEXT,
    jobscript_sha       TEXT REFERENCES blobs(sha),
    command_json        TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS batches (
    id                  INTEGER PRIMARY KEY,
    scenario_version_id INTEGER NOT NULL REFERENCES scenario_versions(id),
    created_at          TEXT NOT NULL,
    instance            TEXT NOT NULL,
    justin_campaign_id  INTEGER,
    jobscript_sha       TEXT REFERENCES blobs(sha),
    n_requested         INTEGER NOT NULL,
    note                TEXT
);
CREATE TABLE IF NOT EXISTS workflows (
    id           INTEGER PRIMARY KEY,
    batch_id     INTEGER NOT NULL REFERENCES batches(id),
    workflow_id  INTEGER NOT NULL,
    submitted_at TEXT NOT NULL,
    command_json TEXT NOT NULL,
    last_state   TEXT,
    last_checked TEXT
);
"""


def now() -> str:
    """Current UTC time as an ISO-8601 string (second resolution)."""
    return datetime.datetime.now(datetime.timezone.utc) \
        .replace(microsecond=0).isoformat()


class DB:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.con = sqlite3.connect(str(path))
        # Rows behave like dicts (row['name']) as well as tuples.
        self.con.row_factory = sqlite3.Row
        self.con.execute('PRAGMA foreign_keys = ON')
        self.con.executescript(SCHEMA)

    # -- generic -----------------------------------------------------------
    def _insert(self, sql: str, args: tuple) -> int:
        """Run an INSERT and commit immediately, returning the new row id.

        Committing per row (rather than per command) is deliberate: if a
        submit of many workflows dies partway, every workflow already created
        in justIN is still recorded here.
        """
        cur = self.con.execute(sql, args)
        self.con.commit()
        return cur.lastrowid

    def query(self, sql: str, args: tuple = ()) -> list:
        return self.con.execute(sql, args).fetchall()

    # -- scenario versions -------------------------------------------------
    def sync_scenario(self, suite: str, name: str, config: dict,
                      config_sha: str):
        """Make sure the current resolved config is recorded.

        Returns (row, created). If this exact config was seen before (same
        hash), the existing version is returned, even if it isn't the newest:
        reverting a YAML edit goes back to the old version rather than making
        a duplicate one.
        """
        row = self.con.execute(
            'SELECT * FROM scenario_versions '
            'WHERE suite=? AND name=? AND config_sha=?',
            (suite, name, config_sha)).fetchone()
        if row:
            return row, False
        last = self.con.execute(
            'SELECT MAX(version) FROM scenario_versions WHERE suite=? AND name=?',
            (suite, name)).fetchone()[0] or 0
        rid = self._insert(
            'INSERT INTO scenario_versions '
            '(suite, name, version, config_json, config_sha, created_at) '
            'VALUES (?,?,?,?,?,?)',
            (suite, name, last + 1, json.dumps(config, sort_keys=True),
             config_sha, now()))
        return self.con.execute('SELECT * FROM scenario_versions WHERE id=?',
                                (rid,)).fetchone(), True

    # -- blobs ---------------------------------------------------------------
    def store_blob(self, text: str) -> str:
        """Store text (if new) and return its sha256."""
        sha = hashlib.sha256(text.encode()).hexdigest()
        self.con.execute('INSERT OR IGNORE INTO blobs (sha, text) VALUES (?,?)',
                         (sha, text))
        self.con.commit()
        return sha

    # -- test runs -----------------------------------------------------------
    def start_test_run(self, version_id: int, dev: bool, command: list,
                       jobscript_sha: str) -> int:
        """Record a test as started (result stays NULL until it finishes, so
        a crashed or killed run is visible as one that never completed)."""
        return self._insert(
            'INSERT INTO test_runs (scenario_version_id, dev, started_at, '
            'command_json, jobscript_sha) VALUES (?,?,?,?,?)',
            (version_id, int(dev), now(), json.dumps(command), jobscript_sha))

    def set_test_log(self, run_id: int, log_path: str):
        """The log name includes the run id, so it's set after the insert."""
        self.con.execute('UPDATE test_runs SET log_path=? WHERE id=?',
                         (log_path, run_id))
        self.con.commit()

    def finish_test_run(self, run_id: int, result: str, detail: list,
                        workdir):
        self.con.execute(
            'UPDATE test_runs SET finished_at=?, result=?, result_detail=?, '
            'workdir=? WHERE id=?',
            (now(), result, json.dumps(detail), workdir, run_id))
        self.con.commit()

    def has_passing_test(self, version_id: int, jobscript_sha: str) -> bool:
        """True if this scenario version passed a non-dev test with this
        exact jobscript. Used by submit to warn about untested configs."""
        return self.con.execute(
            "SELECT 1 FROM test_runs WHERE scenario_version_id=? AND dev=0 "
            "AND result='pass' AND jobscript_sha=? LIMIT 1",
            (version_id, jobscript_sha)).fetchone() is not None

    # -- batches and workflows -------------------------------------------
    def start_batch(self, version_id: int, instance: str, jobscript_sha: str,
                    n_requested: int, note) -> int:
        return self._insert(
            'INSERT INTO batches (scenario_version_id, created_at, instance, '
            'jobscript_sha, n_requested, note) VALUES (?,?,?,?,?,?)',
            (version_id, now(), instance, jobscript_sha, n_requested, note))

    def set_batch_campaign(self, batch_id: int, campaign_id: int):
        self.con.execute('UPDATE batches SET justin_campaign_id=? WHERE id=?',
                         (campaign_id, batch_id))
        self.con.commit()

    def add_workflow(self, batch_id: int, workflow_id: int, command: list):
        return self._insert(
            'INSERT INTO workflows (batch_id, workflow_id, submitted_at, '
            'command_json) VALUES (?,?,?,?)',
            (batch_id, workflow_id, now(), json.dumps(command)))

    def update_workflow_state(self, row_id: int, state: str):
        self.con.execute(
            'UPDATE workflows SET last_state=?, last_checked=? WHERE id=?',
            (state, now(), row_id))
        self.con.commit()

    # -- export ----------------------------------------------------------------
    def dump(self) -> dict:
        """Every table as a list of dicts, for `scale-test export`."""
        out = {}
        for t in ('scenario_versions', 'test_runs', 'batches', 'workflows',
                  'blobs'):
            out[t] = [dict(r) for r in self.query(f'SELECT * FROM {t}')]
        return out
