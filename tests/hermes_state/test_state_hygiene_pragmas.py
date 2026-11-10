"""`database.auto_vacuum` and `database.optimize_on_open` reach the state.db.

Before this, `apply_database_pragmas()` applied five sizing pragmas and one
durability one, and nothing ever set `auto_vacuum` or ran `PRAGMA optimize`:

* `auto_vacuum` stayed at SQLite's default (0 = NONE), so a page freed by a
  delete stayed free FOREVER. The store never shrank on its own: a profile
  that churns (prune old rows, write new ones) grew without bound. Measured on
  the backend profile: 86.669 free pages (327,7 MB, 17,4% of a 1,88 GB file)
  that no amount of waiting would have returned.
* `PRAGMA optimize` (ANALYZE) never ran, so the planner's statistics froze at
  whatever ANALYZE last wrote and a table that grew since (worker_executions,
  worker_receipts) kept a stale row count.

The `auto_vacuum` half has a sharp edge that this module pins: SQLite only
honours a change to `auto_vacuum` on an existing database during a VACUUM.
Setting it on a populated store is accepted and silently does nothing, so a
caller that trusted "no exception" would believe the free pages now come back
automatically -- and they would not. `apply_auto_vacuum` therefore reports the
VERIFIED state and says what makes the change stick.
"""

import json
import sqlite3

import pytest

import hermes_state_wal
from hermes_state import apply_auto_vacuum, apply_database_pragmas, incremental_vacuum


def _config(monkeypatch, database_section):
    """Point apply_database_pragmas at an in-memory config.

    It imports `hermes_cli.config` lazily inside the function body, so the
    patch has to land on that module rather than on a name in this one.
    """
    import hermes_cli.config as config_mod

    cfg = {"database": database_section}
    monkeypatch.setattr(config_mod, "load_config_readonly", lambda *a, **k: cfg)
    return cfg


def _conn(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "state.db"), isolation_level=None)
    conn.row_factory = sqlite3.Row
    return conn


def _auto_vacuum(conn):
    return conn.execute("PRAGMA auto_vacuum").fetchone()[0]


class TestApplyAutoVacuum:
    """The PRAGMA that decides whether freed space ever comes back."""

    def test_fresh_store_takes_the_mode_immediately(self, tmp_path):
        """An EMPTY store honours auto_vacuum at once -- no VACUUM needed."""
        conn = _conn(tmp_path)
        assert _auto_vacuum(conn) == 0  # SQLite's default is NONE
        assert apply_auto_vacuum(conn, 2, db_label="test.db") is True
        assert _auto_vacuum(conn) == 2
        conn.close()

    def test_populated_store_reports_that_it_did_not_take(self, tmp_path, caplog):
        """The sharp edge: on a populated store the PRAGMA is a silent no-op.

        The caller must learn that, not believe it worked.
        """
        conn = _conn(tmp_path)
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
        conn.executemany("INSERT INTO t (v) VALUES (?)", [(f"row-{i}",) for i in range(50)])
        assert _auto_vacuum(conn) == 0

        with caplog.at_level("WARNING"):
            took = apply_auto_vacuum(conn, 2, db_label="test.db")

        assert took is False
        # The file is still NONE -- the PRAGMA did not change it.
        assert _auto_vacuum(conn) == 0
        # And the log says WHY, and what to do about it.
        text = " ".join(r.getMessage() for r in caplog.records)
        assert "did NOT take effect" in text
        assert "VACUUM" in text
        conn.close()

    def test_populated_store_takes_the_mode_after_a_vacuum(self, tmp_path):
        """The documented path: VACUUM rewrites the header, and the mode sticks."""
        conn = _conn(tmp_path)
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
        conn.executemany("INSERT INTO t (v) VALUES (?)", [(f"row-{i}",) for i in range(50)])
        assert apply_auto_vacuum(conn, 2, db_label="test.db") is False

        # A VACUUM is what rewrites the header.
        conn.execute("VACUUM")
        assert apply_auto_vacuum(conn, 2, db_label="test.db") is True
        assert _auto_vacuum(conn) == 2
        conn.close()

    def test_incremental_mode_makes_the_reclaim_cheap(self, tmp_path):
        """INCREMENTAL is what makes the reclaim cheap -- but it does not do it alone.

        Measured on SQLite 3.53.1: with INCREMENTAL set, a DELETE that frees
        pages leaves them on the freelist and the file the same size. The space
        comes back when ``PRAGMA incremental_vacuum`` runs. That is the whole
        point of the mode: the reclaim is a truncation, not a full rewrite.
        """
        conn = _conn(tmp_path)
        assert apply_auto_vacuum(conn, 2, db_label="test.db") is True
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
        conn.executemany("INSERT INTO t (v) VALUES (?)", [(f"row-{i}",) for i in range(5000)])
        size_before = (tmp_path / "state.db").stat().st_size

        conn.execute("DELETE FROM t")
        # The pages are free, but the file has not shrunk yet.
        assert conn.execute("PRAGMA freelist_count").fetchone()[0] > 0

        reclaimed = incremental_vacuum(conn, db_label="test.db")
        assert reclaimed > 0, "incremental_vacuum reclaimed nothing"
        assert conn.execute("PRAGMA freelist_count").fetchone()[0] == 0
        assert (tmp_path / "state.db").stat().st_size < size_before
        conn.close()

    def test_incremental_vacuum_frees_the_whole_freelist_in_one_call(self, tmp_path):
        """Each PRAGMA call frees exactly ONE page, so the helper has to loop.

        Measured on SQLite 3.53.1: freelist 20 -> 19 -> 18, the file one page
        smaller each time -- with NO argument and with a count alike (the count
        is the BUDGET, not a batch size). On a store with 86.669 free pages a
        single call would reclaim one page, so the helper loops.
        """
        conn = _conn(tmp_path)
        apply_auto_vacuum(conn, 2, db_label="test.db")
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
        conn.executemany("INSERT INTO t (v) VALUES (?)", [(f"row-{i}",) for i in range(5000)])
        conn.execute("DELETE FROM t")
        free = conn.execute("PRAGMA freelist_count").fetchone()[0]
        assert free > 1, "need more than one free page for this to mean anything"

        # The bare PRAGMA frees ONE page ...
        conn.execute("PRAGMA incremental_vacuum")
        assert conn.execute("PRAGMA freelist_count").fetchone()[0] == free - 1

        # ... and the helper reclaims the REST in one go.
        reclaimed = incremental_vacuum(conn, db_label="test.db")
        assert reclaimed == free - 1
        assert conn.execute("PRAGMA freelist_count").fetchone()[0] == 0
        conn.close()

    def test_incremental_vacuum_refuses_on_a_none_store(self, tmp_path, caplog):
        """On a NONE store the PRAGMA reclaims nothing -- and must say so.

        SQLite accepts `PRAGMA incremental_vacuum` on a NONE store and does
        nothing (measured: freelist 20 -> 20, the file the same size), so a
        caller that trusted "no exception" would report a reclaim that never
        happened.
        """
        conn = _conn(tmp_path)
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
        conn.executemany("INSERT INTO t (v) VALUES (?)", [(f"row-{i}",) for i in range(5000)])
        conn.execute("DELETE FROM t")
        assert conn.execute("PRAGMA auto_vacuum").fetchone()[0] == 0
        free = conn.execute("PRAGMA freelist_count").fetchone()[0]
        assert free > 0

        with caplog.at_level("WARNING"):
            reclaimed = incremental_vacuum(conn, db_label="test.db")

        assert reclaimed == 0
        text = " ".join(r.getMessage() for r in caplog.records)
        assert "needs auto_vacuum=INCREMENTAL" in text
        # The free pages are still there -- nothing was reclaimed.
        assert conn.execute("PRAGMA freelist_count").fetchone()[0] == free
        conn.close()

    def test_vacuum_picks_up_a_mode_set_on_a_populated_store(self, tmp_path):
        """The documented recovery: the PRAGMA is pending, the VACUUM applies it.

        On a populated store `PRAGMA auto_vacuum=2` is accepted and the header
        still reads 0 -- but the mode is PENDING, and the next VACUUM writes it.
        That is why the warning says "run a VACUUM once, and the mode sticks".
        """
        conn = _conn(tmp_path)
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
        conn.executemany("INSERT INTO t (v) VALUES (?)", [(f"row-{i}",) for i in range(50)])

        # The PRAGMA alone does not change a populated store's mode ...
        assert apply_auto_vacuum(conn, 2, db_label="test.db") is False
        assert _auto_vacuum(conn) == 0

        # ... but the VACUUM writes the pending mode into the header.
        conn.execute("VACUUM")
        assert _auto_vacuum(conn) == 2
        # And from then on the PRAGMA reports success, because it already holds.
        assert apply_auto_vacuum(conn, 2, db_label="test.db") is True
        conn.close()

    def test_out_of_range_level_is_refused(self, tmp_path, caplog):
        conn = _conn(tmp_path)
        with caplog.at_level("WARNING"):
            took = apply_auto_vacuum(conn, 7, db_label="test.db")
        assert took is False
        assert _auto_vacuum(conn) == 0
        assert "out-of-range" in " ".join(r.getMessage() for r in caplog.records)
        conn.close()


class TestApplyDatabasePragmas:
    """The config keys reach the connection."""

    def test_auto_vacuum_from_config_is_applied(self, tmp_path, monkeypatch):
        conn = _conn(tmp_path)
        _config(monkeypatch, {"auto_vacuum": 2})
        apply_database_pragmas(conn, db_label="test.db")
        assert _auto_vacuum(conn) == 2
        conn.close()

    def test_auto_vacuum_absent_from_config_changes_nothing(self, tmp_path, monkeypatch):
        """Unset = SQLite's default. The runtime must not invent a mode."""
        conn = _conn(tmp_path)
        _config(monkeypatch, {})
        apply_database_pragmas(conn, db_label="test.db")
        assert _auto_vacuum(conn) == 0
        conn.close()

    def test_non_integer_auto_vacuum_is_ignored_not_fatal(self, tmp_path, monkeypatch, caplog):
        conn = _conn(tmp_path)
        _config(monkeypatch, {"auto_vacuum": "incremental-ish"})
        with caplog.at_level("WARNING"):
            apply_database_pragmas(conn, db_label="test.db")  # must not raise
        assert _auto_vacuum(conn) == 0
        assert "non-integer database.auto_vacuum" in " ".join(r.getMessage() for r in caplog.records)
        conn.close()

    def test_optimize_on_open_runs_analyze(self, tmp_path, monkeypatch):
        """`PRAGMA optimize` writes the planner's stats into sqlite_stat1.

        SQLite's `PRAGMA optimize` only ANALYZEs what it judges worth analyzing
        -- a table with no index has no plan to pick, so it is skipped. The
        realistic case (a table WITH an index) is what this pins.
        """
        conn = _conn(tmp_path)
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
        conn.execute("CREATE INDEX idx_t_v ON t(v)")
        conn.executemany("INSERT INTO t (v) VALUES (?)", [(f"row-{i}",) for i in range(20)])

        # No ANALYZE has run: the planner has no stats for this index.
        assert conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name='sqlite_stat1'").fetchone()[0] == 0

        _config(monkeypatch, {"optimize_on_open": True})
        apply_database_pragmas(conn, db_label="test.db")

        stat_rows = conn.execute(
            "SELECT COUNT(*) FROM sqlite_stat1 WHERE tbl='t'").fetchone()[0]
        assert stat_rows > 0, "PRAGMA optimize did not ANALYZE the indexed table"
        conn.close()

    def test_optimize_on_open_off_by_default(self, tmp_path, monkeypatch):
        conn = _conn(tmp_path)
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
        conn.executemany("INSERT INTO t (v) VALUES (?)", [(f"row-{i}",) for i in range(20)])

        _config(monkeypatch, {})  # optimize_on_open unset
        apply_database_pragmas(conn, db_label="test.db")

        assert conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name='sqlite_stat1'").fetchone()[0] == 0
        conn.close()

    def test_optimize_failure_does_not_break_the_open(self, tmp_path, monkeypatch):
        """A broken ANALYZE must never take the connection down with it."""
        conn = _conn(tmp_path)
        _config(monkeypatch, {"optimize_on_open": True})

        class _Boom:
            def execute(self, sql):
                if sql == "PRAGMA optimize":
                    raise sqlite3.OperationalError("no such table: nope")
                return conn.execute(sql)

        boom = _Boom()
        apply_database_pragmas(boom, db_label="test.db")  # must not raise
        conn.close()


class TestWorkerExecutionsStatusIndex:
    """The index the status-only probes were missing."""

    def test_status_probe_is_a_search_not_a_scan(self, tmp_path):
        """`WHERE status != 'terminal'` used to SCAN worker_executions.

        The table grows by one row per worker run, so the scan grows with it.
        The index is built from the REAL schema DDL, so this fails if the index
        is ever dropped from the schema.
        """
        from hermes_state_common import SCHEMA_SQL

        conn = _conn(tmp_path)
        conn.executescript(SCHEMA_SQL)
        conn.executemany(
            "INSERT INTO worker_executions (execution_id, session_id, kind, owner_epoch, "
            "generation, status, adoption_digest) VALUES (?, ?, 'kanban', 1, 1, ?, 'd')",
            [(f"exec-{i}", f"sess-{i}", "terminal" if i % 3 else "running")
             for i in range(60)])

        plan = " ".join(
            row[3] for row in conn.execute(
                "EXPLAIN QUERY PLAN SELECT 1 FROM worker_executions WHERE status != 'terminal'"))
        # SQLite reports a full pass over a partial index as
        # "SCAN worker_executions USING COVERING INDEX <name>" -- the index is
        # used, and the query is answered from the index alone (no table row
        # lookup). What must NOT happen is a bare "SCAN worker_executions".
        assert "idx_worker_executions_status" in plan, plan
        assert "SCAN worker_executions" not in plan.replace(
            "SCAN worker_executions USING COVERING INDEX idx_worker_executions_status", ""), plan
        conn.close()

    def test_partial_index_excludes_terminal_rows(self, tmp_path):
        """The index holds only the non-terminal executions.

        A full (status) index also carries every terminal row, and the planner
        then prefers it for the lineage probe -- whose WHERE clause lists
        exactly the non-terminal statuses -- even though that index is not
        covering. Measured on 300k executions, the lineage probe went from
        1,36 ms to 75,12 ms and its plan changed from the covering
        (session_id, status) index to a non-covering (status) one.
        """
        from hermes_state_common import SCHEMA_SQL

        conn = _conn(tmp_path)
        conn.executescript(SCHEMA_SQL)
        conn.executemany(
            "INSERT INTO worker_executions (execution_id, session_id, kind, owner_epoch, "
            "generation, status, adoption_digest) VALUES (?, ?, 'kanban', 1, 1, ?, 'd')",
            [(f"exec-{i}", f"sess-{i}", "terminal" if i % 3 else "running")
             for i in range(60)])
        non_terminal = conn.execute(
            "SELECT COUNT(*) FROM worker_executions WHERE status != 'terminal'").fetchone()[0]
        assert non_terminal == 20, non_terminal

        # The partial predicate is part of the index definition, so a terminal
        # row is simply not in it.
        sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='idx_worker_executions_status'").fetchone()[0]
        assert "terminal" in sql, sql

        indexed = conn.execute(
            "SELECT COUNT(*) FROM worker_executions INDEXED BY idx_worker_executions_status "
            "WHERE status != 'terminal'").fetchone()[0]
        assert indexed == non_terminal, indexed
        conn.close()

    def test_lineage_probe_keeps_the_covering_index(self, tmp_path):
        """The two probes coexist: the status index must not displace the covering one.

        The lineage probe filters on session_id AND a list of non-terminal
        statuses. Only the (session_id, status) index can answer it as a
        covering search; the partial status index carries status but not
        session_id, so if the planner prefers it the probe pays a table lookup
        per matching row. This pins the interaction the two probes have.
        """
        from hermes_state_common import SCHEMA_SQL

        conn = _conn(tmp_path)
        conn.executescript(SCHEMA_SQL)
        conn.executemany(
            "INSERT INTO worker_executions (execution_id, session_id, kind, owner_epoch, "
            "generation, status, adoption_digest) VALUES (?, ?, 'kanban', 1, 1, ?, 'd')",
            [(f"exec-{i}", f"sess-{i}", "terminal" if i % 3 else "running")
             for i in range(60)])

        plan = " ".join(
            row[3] for row in conn.execute(
                "EXPLAIN QUERY PLAN SELECT DISTINCT status FROM worker_executions "
                "WHERE session_id IN (SELECT value FROM json_each(?)) "
                "AND status IN ('registered','running','unknown')",
                (json.dumps(["sess-0", "sess-1"]),)))
        assert "idx_worker_executions_session_status" in plan, plan
        assert "COVERING" in plan, plan
        assert "idx_worker_executions_status" not in plan, plan
        conn.close()
