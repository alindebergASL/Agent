"""Repair proposals supported by recorded tool failures in a completed assignment turn."""

import json


SCHEMA = """
CREATE TABLE IF NOT EXISTS repair_proposals (
    turn_id INTEGER PRIMARY KEY REFERENCES turns(id),
    problem TEXT NOT NULL,
    reproduction TEXT NOT NULL,
    workaround TEXT NOT NULL,
    proposed_change TEXT NOT NULL,
    verification TEXT NOT NULL,
    evidence TEXT NOT NULL,
    published INTEGER NOT NULL DEFAULT 0 CHECK (published IN (0, 1))
);
"""

def eligible_failure(name, result):
    """A local tool reported an error after dispatch. Root cause still needs review."""
    return (
        isinstance(name, str) and bool(name) and name != "stage_repair_proposal" and not name.startswith("mcp_")
        and isinstance(result, dict) and not result.get("error_kind")
        and isinstance(result.get("error"), str) and bool(result["error"].strip())
    )


def failed_calls(connection, task_id, turn_id, call_ids):
    if (not isinstance(call_ids, list) or len(call_ids) != 2
            or any(not isinstance(item, str) or not 1 <= len(item) <= 256 for item in call_ids)
            or len(set(call_ids)) != len(call_ids)):
        raise ValueError("Choose two distinct tool call IDs from this turn.")
    # Only fetch the selected tool names and failure messages. A run's full
    # record can include much larger successful reads and saved reports.
    query = """
        WITH steps AS (
            SELECT id, payload FROM run_steps WHERE task_id=? AND turn_id=?
        ), calls AS (
            SELECT s.id, c.value->>'$.call_id' AS call_id, c.value->>'$.name' AS tool
            FROM steps s, json_each(s.payload, '$.response.output') c
            WHERE s.payload->>'$.type'='response' AND c.value->>'$.type'='function_call'
        ), receipts AS (
            SELECT id, payload->>'$.call_id' AS call_id, payload->>'$.output' AS result
            FROM steps WHERE payload->>'$.type'='function_call_output'
        )
        SELECT calls.call_id, calls.tool,
               CASE WHEN json_type(r.result, '$.error')='text' THEN r.result->>'$.error' END AS error,
               r.result->>'$.error_kind' AS kind
        FROM calls JOIN receipts r ON calls.call_id=r.call_id AND calls.id<r.id
        WHERE calls.call_id IN (SELECT value FROM json_each(?))
        ORDER BY r.id
    """
    rows = connection.execute(query, (task_id, turn_id, json.dumps(call_ids))).fetchall()
    if len(rows) != len(call_ids) or {r["call_id"] for r in rows} != set(call_ids):
        raise ValueError("Each cited call must have one recorded result in the active turn.")
    tools = {row["tool"] for row in rows}
    if len(tools) != 1:
        raise ValueError("Choose failed calls to the same underlying tool.")
    evidence = []
    for row in rows:
        if not eligible_failure(row["tool"], {"error": row["error"], "error_kind": row["kind"]}):
            raise ValueError("Both receipts must show errors returned by the same application tool.")
        evidence.append({key: row[key] for key in ("call_id", "tool", "error")})
    return evidence


class RepairProposalStore:
    def stage_repair_proposal(self, task_id, turn_id, *, problem, reproduction, workaround,
                              proposed_change, verification, call_ids):
        fields = (problem, reproduction, workaround, proposed_change, verification)
        if any(not isinstance(value, str) or not 1 <= len(value.strip()) <= 2000 for value in fields):
            raise ValueError("Complete each proposal field in one to 2000 characters.")
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            active = self._running_turn(connection, task_id, turn_id)
            evidence = failed_calls(connection, task_id, active, call_ids)
            connection.execute(
                """INSERT INTO repair_proposals
                       (turn_id, problem, reproduction, workaround, proposed_change, verification, evidence)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(turn_id) DO UPDATE SET
                       problem=excluded.problem, reproduction=excluded.reproduction,
                       workaround=excluded.workaround, proposed_change=excluded.proposed_change,
                       verification=excluded.verification, evidence=excluded.evidence""",
                (active, *(value.strip() for value in fields), json.dumps(evidence)),
            )
        return {"staged_repair_proposal": True, "turn_id": active, "call_ids": call_ids}

    def list_repair_proposals(self, *, offset=0, limit=50):
        if type(offset) is not int or not 0 <= offset < 2**63 or type(limit) is not int or not 1 <= limit <= 101:
            raise ValueError("Supply a nonnegative offset and a limit from 1 to 101.")
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT p.turn_id, t.task_id, p.problem, p.reproduction, p.workaround,
                          p.proposed_change, p.verification, p.evidence
                   FROM repair_proposals p JOIN turns t ON p.turn_id=t.id
                   WHERE p.published=1 ORDER BY p.turn_id DESC LIMIT ? OFFSET ?""",
                (limit, offset),
            )
            return [{**dict(row), "evidence": json.loads(row["evidence"]),
                     "history_url": f"/tasks/{row['task_id']}/history"} for row in rows]
