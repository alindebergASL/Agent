"""Standing requests, scheduled checks and material findings on the durable assignment queue."""

import json
import uuid
from datetime import datetime

from . import watch_subjects
from .watch_assessment import public_url
from .scheduling import PACIFIC, latest_occurrence, next_occurrence, parse_schedule_time


SCHEMA = """
CREATE TABLE IF NOT EXISTS watches (
    id INTEGER PRIMARY KEY,
    request TEXT NOT NULL,
    cadence TEXT NOT NULL CHECK (cadence IN ('daily','weekly')),
    weekday INTEGER CHECK (weekday BETWEEN 0 AND 6),
    local_time TEXT NOT NULL,
    next_run_at INTEGER NOT NULL,
    stopped_at INTEGER
);
CREATE INDEX IF NOT EXISTS watches_due ON watches(next_run_at) WHERE stopped_at IS NULL;
CREATE TABLE IF NOT EXISTS watch_required_pages (
    watch_id INTEGER NOT NULL REFERENCES watches(id), url TEXT NOT NULL,
    PRIMARY KEY(watch_id,url)
);
CREATE TABLE IF NOT EXISTS watch_checks (
    id INTEGER PRIMARY KEY,
    watch_id INTEGER NOT NULL REFERENCES watches(id),
    task_id TEXT NOT NULL UNIQUE REFERENCES tasks(id),
    due_at INTEGER NOT NULL,
    manual INTEGER NOT NULL,
    outcome TEXT CHECK (outcome IN ('baseline','material','unchanged','unavailable')),
    summary TEXT,
    evidence TEXT,
    findings TEXT
);
CREATE INDEX IF NOT EXISTS watch_history ON watch_checks(watch_id,id);
CREATE UNIQUE INDEX IF NOT EXISTS watch_due_once ON watch_checks(watch_id,due_at) WHERE manual=0;
CREATE TABLE IF NOT EXISTS staged_watch_assessments (
    turn_id INTEGER PRIMARY KEY REFERENCES turns(id),
    outcome TEXT NOT NULL,
    summary TEXT NOT NULL,
    evidence TEXT NOT NULL,
    findings TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS watch_findings (
    id INTEGER PRIMARY KEY,
    watch_id INTEGER NOT NULL REFERENCES watches(id),
    task_id TEXT NOT NULL REFERENCES watch_checks(task_id),
    event_key TEXT NOT NULL,
    title TEXT NOT NULL,
    why TEXT NOT NULL,
    evidence TEXT NOT NULL,
    acknowledged INTEGER NOT NULL DEFAULT 0,
    UNIQUE(watch_id,event_key)
);
CREATE INDEX IF NOT EXISTS watch_finding_history ON watch_findings(watch_id,id);
CREATE INDEX IF NOT EXISTS watch_unread ON watch_findings(id) WHERE acknowledged=0;
"""


class WatchError(ValueError):
    pass


def occurrence(weekday, local_time, now, *, upcoming):
    """A null weekday recurs every day. Reuse the weekly DST gap/fold rules."""
    compute = next_occurrence if upcoming else latest_occurrence
    if weekday is not None:
        return compute(weekday, local_time, now)
    today = now.astimezone(PACIFIC).weekday()
    adjacent = (today + (1 if upcoming else -1)) % 7
    values = (compute(today, local_time, now), compute(adjacent, local_time, now))
    return min(values) if upcoming else max(values)


def check_record(row):
    if row is None:
        return None
    check = dict(row)
    for field in ("evidence", "findings"):
        check[field] = json.loads(check[field]) if check[field] else []
    return check


CHECK_SELECT = """
    SELECT c.id,c.watch_id,c.task_id,c.due_at,c.manual,c.outcome,c.summary,c.evidence,c.findings,
           CASE WHEN c.outcome IS NULL THEN t.status ELSE 'completed' END AS status,
           CASE WHEN c.outcome IS NULL THEN t.error END AS error
    FROM watch_checks c JOIN tasks t ON t.id=c.task_id
"""


def memory_excerpt(text, limit):
    if text is None or len(text) <= limit:
        return text
    return text[:limit - 1] + "…"


def memory_evidence(encoded):
    # Keep complete reopenable URLs. Never truncate an identifier to fit a prompt.
    urls = []
    used = 0
    for url in json.loads(encoded or "[]"):
        if used + len(url) > 2400 or len(urls) == 4:
            break
        urls.append(url)
        used += len(url)
    return urls


def check_memory(row):
    if row is None:
        return None
    return {"task_id": row["task_id"], "outcome": row["outcome"], "due_at": row["due_at"],
            "status": row["status"], "summary": memory_excerpt(row["summary"], 1800),
            "evidence": memory_evidence(row["evidence"]), "error": memory_excerpt(row["error"], 800)}


def required_pages(connection, watch_id):
    return [row[0] for row in connection.execute(
        "SELECT url FROM watch_required_pages WHERE watch_id=? ORDER BY url", (watch_id,))]


def context(connection, task_id):
    watch = connection.execute(
        """SELECT c.id AS check_id,c.watch_id,c.due_at,c.manual,w.request
           FROM watch_checks c JOIN watches w ON w.id=c.watch_id
           WHERE c.task_id=? AND c.outcome IS NULL""", (task_id,)
    ).fetchone()
    if watch is None:
        return None
    args = (watch["watch_id"], watch["check_id"])
    history = CHECK_SELECT + " WHERE c.watch_id=? AND c.id<?"
    previous = connection.execute(history + """
        AND c.outcome IN ('baseline','material','unchanged') ORDER BY c.id DESC LIMIT 1""", args).fetchone()
    baseline = connection.execute(history + """
        AND c.outcome IN ('baseline','material') ORDER BY c.id ASC LIMIT 1""", args).fetchone()
    attempt = connection.execute(history + " ORDER BY c.id DESC LIMIT 1", args).fetchone()
    # The prompt has a byte budget; stored history and deduplication retain every finding.
    rows = connection.execute("""
        SELECT f.task_id,f.event_key AS key,f.title,f.why,f.evidence
        FROM watch_findings f JOIN watch_checks c ON c.task_id=f.task_id
        WHERE f.watch_id=? AND c.id<? ORDER BY f.id DESC""", args)
    events, remaining = [], 8192
    has_older_findings = False
    for row in rows:
        event = {**dict(row), "why": memory_excerpt(row["why"], 320), "evidence": memory_evidence(row["evidence"])}
        size = len(json.dumps(event, ensure_ascii=False).encode("utf-8"))
        if size > remaining:
            has_older_findings = True
            break
        events.append(event)
        remaining -= size + 2
    return {**dict(watch), "required_urls": required_pages(connection, watch["watch_id"]),
            "subject": watch_subjects.load(connection, watch["watch_id"]),
            "baseline": check_memory(baseline), "previous": check_memory(previous),
            "previous_attempt": check_memory(attempt), "known_events": events, "has_older_findings": has_older_findings}



def publish(connection, task_id, turn_id):
    check = connection.execute(
        "SELECT id,watch_id FROM watch_checks WHERE task_id=? AND outcome IS NULL", (task_id,)
    ).fetchone()
    if check is None:
        return
    draft = connection.execute("SELECT * FROM staged_watch_assessments WHERE turn_id=?", (turn_id,)).fetchone()
    if draft is None:
        raise WatchError("This watch check finished without a recorded assessment. Check again.")
    if connection.execute(
        """SELECT 1 FROM watch_checks WHERE watch_id=? AND id>? AND outcome IN
           ('baseline','material','unchanged') LIMIT 1""", (check["watch_id"], check["id"])
    ).fetchone():
        raise WatchError("A newer watch check has already completed. Its findings take precedence.")

    new_findings = []
    for finding in json.loads(draft["findings"]):
        cursor = connection.execute(
            """INSERT INTO watch_findings(watch_id,task_id,event_key,title,why,evidence)
               VALUES(?,?,?,?,?,?) ON CONFLICT(watch_id,event_key) DO NOTHING""",
            (check["watch_id"], task_id, finding["key"], finding["title"], finding["why"],
             json.dumps(finding["evidence"])),
        )
        if cursor.rowcount:
            new_findings.append(finding)
    outcome, summary = draft["outcome"], draft["summary"]
    if outcome == "material" and not new_findings:
        outcome = "unchanged"
        summary = "These findings were already reported for this watch. " + summary
    connection.execute(
        "UPDATE watch_checks SET outcome=?,summary=?,evidence=?,findings=? WHERE id=?",
        (outcome, summary, draft["evidence"], json.dumps(new_findings), check["id"]),
    )
    return outcome


class WatchStore:
    def create_watch(self, request, cadence, weekday, local_time, *, now: datetime, subject=None, required_urls=()):
        if not isinstance(request, str) or not 1 <= len(request.strip()) <= 8000 or "\x00" in request:
            raise ValueError("Enter what to watch for in up to 8,000 characters.")
        if cadence not in ("daily", "weekly"):
            raise ValueError("Choose a daily or weekly check.")
        if cadence == "weekly" and (type(weekday) is not int or not 0 <= weekday <= 6):
            raise ValueError("Choose a weekday for a weekly watch.")
        weekday = None if cadence == "daily" else weekday
        at = parse_schedule_time(local_time)
        if not isinstance(required_urls, (list, tuple)) or len(required_urls) > 8:
            raise ValueError("Specify at most eight required public pages.")
        pages = {public_url(url) for url in required_urls}
        if None in pages or any(len(url) > 2048 for url in pages):
            raise ValueError("Enter a complete HTTP or HTTPS URL of up to 2,048 characters for each required page.")
        self._schedule_epoch(now)
        with self._connection() as conn:
            watch_id = conn.execute(
                """INSERT INTO watches(request,cadence,weekday,local_time,next_run_at)
                   VALUES(?,?,?,?,?) RETURNING id""",
                (request.strip(), cadence, weekday, at.isoformat(timespec="minutes"),
                 occurrence(weekday, at, now, upcoming=True)),
            ).fetchone()["id"]
            if subject:
                watch_subjects.attach(conn, watch_id, subject)
            conn.executemany("INSERT INTO watch_required_pages(watch_id,url) VALUES(?,?)",
                             ((watch_id, url) for url in pages))
            return watch_id

    def list_watches(self):
        with self._connection() as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM watches ORDER BY stopped_at IS NOT NULL,id DESC")]

    def get_watch(self, watch_id):
        with self._connection() as conn:
            conn.execute("BEGIN")
            row = conn.execute("SELECT * FROM watches WHERE id=?", (watch_id,)).fetchone()
            if row is None:
                return None
            history = conn.execute(CHECK_SELECT + " WHERE c.watch_id=? ORDER BY c.id DESC", (watch_id,))
            return {**dict(row), "required_urls": required_pages(conn, watch_id),
                    "subject": watch_subjects.load(conn, watch_id),
                    "checks": [check_record(r) for r in history]}

    def _queue_watch(self, conn, watch, due_at, *, manual):
        active = conn.execute("""
            SELECT c.task_id FROM watch_checks c JOIN tasks t ON t.id=c.task_id
            WHERE c.watch_id=? AND c.outcome IS NULL AND t.status IN ('queued','running','waiting')
            ORDER BY c.id DESC LIMIT 1""", (watch["id"],)).fetchone()
        if active is not None:
            return active["task_id"]
        task = uuid.uuid4().hex
        self._insert_task(conn, task, watch["request"])
        conn.execute("INSERT INTO watch_checks(watch_id,task_id,due_at,manual) VALUES(?,?,?,?)",
                     (watch["id"], task, due_at, int(manual)))
        watch_subjects.pin(self, conn, watch["id"], task)
        return task

    def enqueue_watch_now(self, watch_id, *, now):
        epoch = self._schedule_epoch(now)
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            watch = conn.execute("SELECT * FROM watches WHERE id=?", (watch_id,)).fetchone()
            if watch is None:
                raise KeyError(watch_id)
            if watch["stopped_at"] is not None:
                raise ValueError("This watch has been stopped.")
            return self._queue_watch(conn, watch, epoch, manual=True)

    def enqueue_due_watches(self, *, now):
        epoch = self._schedule_epoch(now)
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            # An existing queued check can incorporate the due occurrence. A running check
            # may already have read its sources, and a waiting one is paused. Preserve
            # that due time until either check finishes so new evidence is not skipped.
            due = conn.execute("""
                SELECT * FROM watches w WHERE w.stopped_at IS NULL AND w.next_run_at<=?
                    AND NOT EXISTS (
                        SELECT 1 FROM watch_checks c JOIN tasks t ON t.id=c.task_id
                        WHERE c.watch_id=w.id AND c.outcome IS NULL AND t.status IN ('running','waiting')
                    )
                ORDER BY w.next_run_at,w.id""", (epoch,)).fetchall()
            queued = []
            for watch in due:
                at = parse_schedule_time(watch["local_time"])
                instant = occurrence(watch["weekday"], at, now, upcoming=False)
                queued.append(self._queue_watch(conn, watch, instant, manual=False))
                conn.execute("UPDATE watches SET next_run_at=? WHERE id=?",
                             (occurrence(watch["weekday"], at, now, upcoming=True), watch["id"]))
            return queued

    def stop_watch(self, watch_id, *, now):
        epoch = self._schedule_epoch(now)
        with self._connection() as conn:
            if not conn.execute("UPDATE watches SET stopped_at=COALESCE(stopped_at,?) WHERE id=?",
                                (epoch, watch_id)).rowcount:
                raise KeyError(watch_id)

    def stage_watch_assessment(self, task_id, turn_id, *, outcome, summary, findings, evidence):
        if outcome not in ("baseline", "material", "unchanged", "unavailable"):
            raise ValueError("Choose a supported watch outcome.")
        if not isinstance(summary, str) or not summary.strip() or len(summary) > 4000:
            raise ValueError("Summarize this check in 1 to 4,000 characters.")
        if not isinstance(findings, list) or len(findings) > 16 or bool(findings) != (outcome == "material"):
            raise ValueError("Include up to 16 findings for a material outcome only.")

        def sources(values):
            if (not isinstance(values, list) or len(values) > 24
                    or any(not isinstance(url, str) or not 1 <= len(url) <= 2048 for url in values)):
                raise ValueError("Use a list of up to 24 evidence links.")
            return list(dict.fromkeys(values))

        evidence = sources(evidence)
        if not evidence and outcome != "unavailable":
            raise ValueError("Include the evidence checked for this assessment.")
        clean_findings = []
        for finding in findings:
            if not isinstance(finding, dict) or set(finding) != {"key", "title", "why", "evidence"}:
                raise ValueError("Each finding needs a stable key, title, reason and evidence links.")
            for key, limit in (("key", 240), ("title", 300), ("why", 1500)):
                if not isinstance(finding[key], str) or not 1 <= len(finding[key].strip()) <= limit:
                    raise ValueError(f"Enter a {key} of 1 to {limit} characters for each finding.")
            links = sources(finding["evidence"])
            if not links:
                raise ValueError("Each material finding needs supporting evidence.")
            clean_findings.append({"key": " ".join(finding["key"].casefold().split()), "title": finding["title"].strip(),
                                   "why": finding["why"].strip(), "evidence": links})
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._running_turn(conn, task_id, turn_id)
            check = conn.execute("SELECT watch_id FROM watch_checks WHERE task_id=? AND outcome IS NULL",
                                 (task_id,)).fetchone()
            if check is None:
                raise ValueError("There is no open watch check for this assignment.")
            conn.execute("""
                INSERT INTO staged_watch_assessments(turn_id,outcome,summary,evidence,findings) VALUES(?,?,?,?,?)
                ON CONFLICT(turn_id) DO UPDATE SET outcome=excluded.outcome,summary=excluded.summary,
                    evidence=excluded.evidence,findings=excluded.findings""",
                (turn_id, outcome, summary.strip(), json.dumps(evidence), json.dumps(clean_findings)))

    def watch_attention(self):
        with self._connection() as conn:
            rows = conn.execute("""
                SELECT f.id,f.watch_id,w.request,f.task_id,c.due_at,f.event_key AS key,f.title,f.why,f.evidence
                FROM watch_findings f JOIN watches w ON w.id=f.watch_id JOIN watch_checks c ON c.task_id=f.task_id
                WHERE f.acknowledged=0 ORDER BY f.id DESC""")
            return [{**dict(row), "evidence": json.loads(row["evidence"])} for row in rows]

    def acknowledge_watch_notice(self, watch_id, notice_id):
        with self._connection() as conn:
            if not conn.execute("UPDATE watch_findings SET acknowledged=1 WHERE watch_id=? AND id=?",
                                (watch_id, notice_id)).rowcount:
                raise KeyError(notice_id)
