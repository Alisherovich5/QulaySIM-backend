"""The audit log has to be trimmed, and has to survive not existing.

`audit_row_change` is written by triggers installed straight into Postgres, so
no ORM model describes it and nothing in a migration creates it. Two things
follow, and both are tested here: the task cannot assume the table is there,
and nothing else will delete an old row if this does not.

Why it matters: unattended, the table reached 31 MB — larger than the whole
catalogue — and doubled the size of every nightly dump.
"""

from __future__ import annotations

import pytest

from app.workers.tasks import maintenance


class _Result:
    def __init__(self, scalar=None, rowcount=0):
        self._scalar = scalar
        self.rowcount = rowcount

    def scalar(self):
        return self._scalar


class _Session:
    """Answers the existence probe, then counts the delete."""

    def __init__(self, table_exists: bool, removed: int = 0):
        self.table_exists = table_exists
        self.removed = removed
        self.statements: list[str] = []
        self.params: list[dict] = []
        self.committed = False

    def execute(self, statement, params=None):
        self.statements.append(str(statement))
        if params:
            self.params.append(params)
        if "to_regclass" in str(statement):
            return _Result(scalar=self.table_exists)
        return _Result(rowcount=self.removed)

    def connection(self):
        # SQLAlchemy's Session.execute is typed without a rowcount; the task
        # goes through the connection for it, so the double does too.
        return self

    def commit(self):
        self.committed = True

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


@pytest.fixture
def session(monkeypatch):
    def _install(table_exists: bool, removed: int = 0) -> _Session:
        made = _Session(table_exists, removed)
        monkeypatch.setattr(maintenance, "worker_session", lambda: made)
        return made

    return _install


def test_it_deletes_rows_past_the_window(session) -> None:
    made = session(True, removed=412)
    assert maintenance.trim_audit_log() == 412
    assert made.committed
    assert any("DELETE FROM audit_row_change" in s for s in made.statements)


def test_the_window_is_the_one_that_is_documented(session) -> None:
    """The number is in the task, not scattered across a cron line and a doc."""
    made = session(True, removed=1)
    maintenance.trim_audit_log()
    assert made.params[-1] == {"days": maintenance.AUDIT_RETENTION_DAYS}
    assert maintenance.AUDIT_RETENTION_DAYS >= 30


def test_a_database_without_the_table_is_not_an_error(session) -> None:
    """A restore from before the triggers existed is a valid database, and a
    housekeeping task that raises on it takes the whole beat schedule with it."""
    made = session(False)
    assert maintenance.trim_audit_log() == 0
    assert not any("DELETE" in s for s in made.statements)


def test_it_never_deletes_everything(session) -> None:
    """The delete is bounded by age, not a truncate: the recent rows are the
    ones an investigation actually reads."""
    made = session(True, removed=5)
    maintenance.trim_audit_log()
    deletes = [s for s in made.statements if "DELETE" in s]
    assert deletes and all("WHERE at <" in s for s in deletes)
