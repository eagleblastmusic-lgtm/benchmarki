"""Read-only Work Kernel query adapter for projection-only consumers.

The mutable WorkKernelStore remains the sole M4a lifecycle writer. This
adapter is given an already verified SQLite read-only connection and recreates
the exact canonical WorkItemQuery DTO; consumers never interpret raw Work
tables themselves.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Callable, Mapping
from typing import Any, NoReturn

from bdb_vnext.m4a_work_kernel import (
    LeaseRecord,
    M4A_MAX_FACT_BYTES,
    ResourceClaim,
    RunRecord,
    TransitionFact,
    WaitRecord,
    WorkItem,
    WorkItemQuery,
)


class M4aReadQueryError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _fail(code: str, message: str) -> NoReturn:
    raise M4aReadQueryError(code, message)


class ReadOnlyWorkKernelQuery:
    """Canonical M4a DTO reader over an externally read-only connection."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._connection = connection
        self._clock = clock or time.time

    @staticmethod
    def _work(row: tuple[Any, ...]) -> WorkItem:
        return WorkItem(
            str(row[0]), str(row[1]), str(row[2]), str(row[3]),
            int(row[4]), int(row[5]), int(row[6]), float(row[7]), float(row[8])
        )

    @staticmethod
    def _run(row: tuple[Any, ...]) -> RunRecord:
        return RunRecord(
            str(row[0]), str(row[1]), str(row[2]),
            None if row[3] is None else str(row[3]),
            str(row[4]), str(row[5]), int(row[6]), int(row[7]),
            None if row[8] is None else int(row[8]),
        )

    @staticmethod
    def _wait(row: tuple[Any, ...]) -> WaitRecord:
        return WaitRecord(
            str(row[0]), str(row[1]), str(row[2]), str(row[3]),
            int(row[4]), None if row[5] is None else int(row[5]),
            None if row[6] is None else str(row[6]),
        )

    @staticmethod
    def _lease(row: tuple[Any, ...], *, now: float) -> LeaseRecord:
        state = (
            "RELEASED"
            if str(row[4]) != "ACTIVE"
            else ("EXPIRED" if float(row[6]) <= now else "ACTIVE")
        )
        return LeaseRecord(
            str(row[1]), str(row[0]), str(row[2]), int(row[3]), state,
            float(row[5]), float(row[6])
        )

    @staticmethod
    def _claim(row: tuple[Any, ...]) -> ResourceClaim:
        return ResourceClaim(
            str(row[0]), str(row[1]), str(row[2]), int(row[3]),
            str(row[4]), int(row[5])
        )

    @staticmethod
    def _fact(row: tuple[Any, ...]) -> TransitionFact:
        raw = bytes(row[7])
        if len(raw) > M4A_MAX_FACT_BYTES:
            _fail("fact_too_large", "transition fact exceeds the bounded M4a size")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise M4aReadQueryError("fact_corrupt", "transition fact payload is invalid JSON") from exc
        if not isinstance(payload, Mapping):
            _fail("fact_corrupt", "transition fact payload is not an object")
        return TransitionFact(
            str(row[0]), str(row[1]), int(row[2]), str(row[3]),
            None if row[4] is None else str(row[4]),
            None if row[5] is None else str(row[5]),
            str(row[6]), dict(payload), int(row[8]),
        )

    def work_ids(self, *, limit: int) -> tuple[str, ...]:
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 5_000:
            _fail("invalid_query_limit", "Work query limit is out of bounds")
        try:
            rows = self._connection.execute(
                "SELECT work_id FROM m4a_work_items "
                "ORDER BY updated_order DESC,work_id LIMIT ?",
                (limit,),
            ).fetchall()
        except sqlite3.DatabaseError as exc:
            raise M4aReadQueryError("database_read_failed", "M4a Work catalog could not be read") from exc
        return tuple(str(row[0]) for row in rows)

    def query(self, work_id: str, *, now: float | None = None) -> WorkItemQuery | None:
        if not isinstance(work_id, str) or not work_id or len(work_id) > 192 or "\x00" in work_id:
            _fail("invalid_identifier", "work_id must be a bounded identifier")
        timestamp = float(self._clock() if now is None else now)
        if timestamp != timestamp or timestamp in {float("inf"), float("-inf")}:
            _fail("invalid_time", "now must be finite")
        try:
            row = self._connection.execute(
                "SELECT work_id,task_id,kind,disposition,state_version,created_order,"
                "updated_order,created_at,updated_at FROM m4a_work_items WHERE work_id=?",
                (work_id,),
            ).fetchone()
            if row is None:
                return None
            active_row = self._connection.execute(
                "SELECT run_id,work_id,status,outcome,effect_certainty,lease_id,fence,"
                "started_order,ended_order FROM m4a_runs WHERE work_id=? AND status='ACTIVE' "
                "ORDER BY started_order DESC LIMIT 1",
                (work_id,),
            ).fetchone()
            last_row = self._connection.execute(
                "SELECT run_id,work_id,status,outcome,effect_certainty,lease_id,fence,"
                "started_order,ended_order FROM m4a_runs WHERE work_id=? "
                "ORDER BY started_order DESC LIMIT 1",
                (work_id,),
            ).fetchone()
            wait_row = self._connection.execute(
                "SELECT wait_id,work_id,reason,status,created_order,resolved_order,resolution "
                "FROM m4a_waits WHERE work_id=? AND status='OPEN' "
                "ORDER BY created_order DESC LIMIT 1",
                (work_id,),
            ).fetchone()
            lease_row = self._connection.execute(
                "SELECT work_id,lease_id,owner_id,fence,state,acquired_at,expires_at "
                "FROM m4a_leases WHERE work_id=?",
                (work_id,),
            ).fetchone()
            claim_row = self._connection.execute(
                "SELECT resource_key,work_id,lease_id,fence,state,claimed_order "
                "FROM m4a_resource_claims WHERE work_id=? AND state='HELD' "
                "ORDER BY claimed_order DESC LIMIT 1",
                (work_id,),
            ).fetchone()
            fact_rows = self._connection.execute(
                "SELECT fact_id,work_id,state_version,kind,from_disposition,to_disposition,"
                "causal_digest,payload,created_order FROM m4a_transition_facts "
                "WHERE work_id=? ORDER BY state_version DESC LIMIT 8",
                (work_id,),
            ).fetchall()
        except sqlite3.DatabaseError as exc:
            raise M4aReadQueryError("database_read_failed", "canonical M4a Work query failed") from exc
        return WorkItemQuery(
            self._work(tuple(row)),
            self._run(tuple(active_row)) if active_row else None,
            self._run(tuple(last_row)) if last_row else None,
            self._wait(tuple(wait_row)) if wait_row else None,
            self._lease(tuple(lease_row), now=timestamp) if lease_row else None,
            self._claim(tuple(claim_row)) if claim_row else None,
            tuple(self._fact(tuple(item)) for item in reversed(fact_rows)),
        )

    def catalog(self, *, limit: int) -> tuple[WorkItemQuery, ...]:
        result: list[WorkItemQuery] = []
        now = float(self._clock())
        for work_id in self.work_ids(limit=limit):
            query = self.query(work_id, now=now)
            if query is None:
                _fail("work_catalog_unstable", "WorkItem disappeared during read-only catalog")
            result.append(query)
        return tuple(result)


__all__ = ["M4aReadQueryError", "ReadOnlyWorkKernelQuery"]
