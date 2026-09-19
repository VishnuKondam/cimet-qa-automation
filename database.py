"""SQLite persistence layer. All access goes through aiosqlite for async FastAPI handlers."""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import aiosqlite

DB_PATH = Path(__file__).parent / "data" / "qa_automation.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
    id TEXT PRIMARY KEY,
    retailer_id TEXT NOT NULL,
    agent_id TEXT,
    call_ts TEXT,
    crm_payload TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING',
    card_data_violation INTEGER NOT NULL DEFAULT 0,
    sampled_clean INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS transcripts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id TEXT NOT NULL REFERENCES leads(id),
    speaker TEXT NOT NULL,
    text TEXT NOT NULL,
    start_ts REAL NOT NULL,
    end_ts REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS check_library (
    id TEXT NOT NULL,
    retailer_id TEXT NOT NULL,
    check_type TEXT NOT NULL,
    description TEXT NOT NULL,
    script_text TEXT,
    is_critical INTEGER NOT NULL DEFAULT 0,
    weight REAL NOT NULL DEFAULT 1.0,
    effective_from TEXT NOT NULL,
    PRIMARY KEY (id, retailer_id, effective_from)
);

CREATE TABLE IF NOT EXISTS evaluations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id TEXT NOT NULL REFERENCES leads(id),
    check_id TEXT NOT NULL,
    check_version_effective_from TEXT NOT NULL,
    status TEXT NOT NULL,
    transcript_quote TEXT,
    timestamp REAL,
    expected_value TEXT,
    reasoning TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS overrides (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    evaluation_id INTEGER NOT NULL REFERENCES evaluations(id),
    original_status TEXT NOT NULL,
    new_status TEXT NOT NULL,
    note TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS scores (
    lead_id TEXT PRIMARY KEY REFERENCES leads(id),
    score_with_fatal REAL NOT NULL,
    score_without_fatal REAL NOT NULL,
    computed_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


async def get_db() -> aiosqlite.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = await aiosqlite.connect(DB_PATH)
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA foreign_keys = ON")
    return conn


async def init_db() -> None:
    conn = await get_db()
    try:
        await conn.executescript(SCHEMA)
        await conn.commit()
    finally:
        await conn.close()


async def seed_check_library(retailer_id: str, checks: list[dict[str, Any]]) -> None:
    conn = await get_db()
    try:
        for c in checks:
            await conn.execute(
                """INSERT OR REPLACE INTO check_library
                   (id, retailer_id, check_type, description, script_text, is_critical, weight, effective_from)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    c["id"], retailer_id, c["check_type"], c["description"], c.get("script_text"),
                    int(c["is_critical"]), c.get("weight", 1.0), c["effective_from"],
                ),
            )
        await conn.commit()
    finally:
        await conn.close()


async def get_active_checks(retailer_id: str, call_date: str) -> list[aiosqlite.Row]:
    """Resolve each check to the version that was live on call_date, not today's version."""
    conn = await get_db()
    try:
        cursor = await conn.execute(
            """
            SELECT cl.* FROM check_library cl
            INNER JOIN (
                SELECT id, MAX(effective_from) AS latest
                FROM check_library
                WHERE retailer_id = ? AND effective_from <= ?
                GROUP BY id
            ) latest ON cl.id = latest.id AND cl.effective_from = latest.latest
            WHERE cl.retailer_id = ?
            """,
            (retailer_id, call_date, retailer_id),
        )
        rows = await cursor.fetchall()
        return rows
    finally:
        await conn.close()


async def insert_lead(lead_id: str, retailer_id: str, agent_id: Optional[str],
                       call_ts: str, crm_payload: dict) -> None:
    conn = await get_db()
    try:
        await conn.execute(
            """INSERT OR REPLACE INTO leads (id, retailer_id, agent_id, call_ts, crm_payload, status)
               VALUES (?, ?, ?, ?, ?, 'PENDING')""",
            (lead_id, retailer_id, agent_id, call_ts, json.dumps(crm_payload)),
        )
        await conn.commit()
    finally:
        await conn.close()


async def insert_transcript_segments(lead_id: str, segments: list[dict]) -> None:
    conn = await get_db()
    try:
        await conn.execute("DELETE FROM transcripts WHERE lead_id = ?", (lead_id,))
        await conn.executemany(
            "INSERT INTO transcripts (lead_id, speaker, text, start_ts, end_ts) VALUES (?, ?, ?, ?, ?)",
            [(lead_id, s["speaker"], s["text"], s["start_ts"], s["end_ts"]) for s in segments],
        )
        await conn.commit()
    finally:
        await conn.close()


async def get_transcript(lead_id: str) -> list[aiosqlite.Row]:
    conn = await get_db()
    try:
        cursor = await conn.execute(
            "SELECT * FROM transcripts WHERE lead_id = ? ORDER BY start_ts ASC", (lead_id,)
        )
        return await cursor.fetchall()
    finally:
        await conn.close()


async def insert_evaluations(lead_id: str, evaluations: list[dict]) -> None:
    conn = await get_db()
    try:
        await conn.execute("DELETE FROM evaluations WHERE lead_id = ?", (lead_id,))
        await conn.executemany(
            """INSERT INTO evaluations
               (lead_id, check_id, check_version_effective_from, status, transcript_quote,
                timestamp, expected_value, reasoning)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (
                    lead_id, e["check_id"], e["check_version_effective_from"], e["status"],
                    e.get("transcript_quote"), e.get("timestamp"), e.get("expected_value"),
                    e.get("reasoning"),
                )
                for e in evaluations
            ],
        )
        await conn.commit()
    finally:
        await conn.close()


async def update_lead_status(lead_id: str, status: str, card_violation: bool, sampled_clean: bool) -> None:
    conn = await get_db()
    try:
        await conn.execute(
            "UPDATE leads SET status = ?, card_data_violation = ?, sampled_clean = ? WHERE id = ?",
            (status, int(card_violation), int(sampled_clean), lead_id),
        )
        await conn.commit()
    finally:
        await conn.close()


async def upsert_scores(lead_id: str, score_with_fatal: float, score_without_fatal: float) -> None:
    conn = await get_db()
    try:
        await conn.execute(
            """INSERT OR REPLACE INTO scores (lead_id, score_with_fatal, score_without_fatal)
               VALUES (?, ?, ?)""",
            (lead_id, score_with_fatal, score_without_fatal),
        )
        await conn.commit()
    finally:
        await conn.close()


async def get_lead_bundle(lead_id: str) -> Optional[dict]:
    """Everything the frontend needs for one lead: CRM data, transcript, evaluations, scores."""
    conn = await get_db()
    try:
        lead_row = await (await conn.execute("SELECT * FROM leads WHERE id = ?", (lead_id,))).fetchone()
        if lead_row is None:
            return None
        transcript_rows = await (await conn.execute(
            "SELECT * FROM transcripts WHERE lead_id = ? ORDER BY start_ts ASC", (lead_id,)
        )).fetchall()
        eval_rows = await (await conn.execute(
            """SELECT e.*, cl.description, cl.check_type, cl.is_critical, cl.weight
               FROM evaluations e
               LEFT JOIN check_library cl
                 ON cl.id = e.check_id AND cl.effective_from = e.check_version_effective_from
                 AND cl.retailer_id = ?
               WHERE e.lead_id = ?""",
            (lead_row["retailer_id"], lead_id),
        )).fetchall()
        score_row = await (await conn.execute("SELECT * FROM scores WHERE lead_id = ?", (lead_id,))).fetchone()
        override_rows = await (await conn.execute(
            """SELECT o.* FROM overrides o
               INNER JOIN evaluations e ON e.id = o.evaluation_id
               WHERE e.lead_id = ?""",
            (lead_id,),
        )).fetchall()
        return {
            "lead": dict(lead_row),
            "transcript": [dict(r) for r in transcript_rows],
            "evaluations": [dict(r) for r in eval_rows],
            "score": dict(score_row) if score_row else None,
            "overrides": [dict(r) for r in override_rows],
        }
    finally:
        await conn.close()


async def count_recent_critical_fails(agent_id: str, check_id: str, since_iso: str) -> int:
    """Used for repeat-offence detection: same critical check failing 3+ times in rolling 7 days."""
    conn = await get_db()
    try:
        cursor = await conn.execute(
            """SELECT COUNT(*) AS n FROM evaluations e
               INNER JOIN leads l ON l.id = e.lead_id
               WHERE l.agent_id = ? AND e.check_id = ? AND e.status = 'FAIL' AND e.created_at >= ?""",
            (agent_id, check_id, since_iso),
        )
        row = await cursor.fetchone()
        return row["n"] if row else 0
    finally:
        await conn.close()


async def get_evaluation(evaluation_id: int) -> Optional[aiosqlite.Row]:
    conn = await get_db()
    try:
        cursor = await conn.execute("SELECT * FROM evaluations WHERE id = ?", (evaluation_id,))
        return await cursor.fetchone()
    finally:
        await conn.close()


async def get_lead(lead_id: str) -> Optional[aiosqlite.Row]:
    conn = await get_db()
    try:
        cursor = await conn.execute("SELECT * FROM leads WHERE id = ?", (lead_id,))
        return await cursor.fetchone()
    finally:
        await conn.close()


async def get_evaluations_for_lead(lead_id: str) -> list[aiosqlite.Row]:
    conn = await get_db()
    try:
        cursor = await conn.execute("SELECT * FROM evaluations WHERE lead_id = ?", (lead_id,))
        return await cursor.fetchall()
    finally:
        await conn.close()


async def overturn_evaluation(lead_id: str, check_id: str, reason: str) -> bool:
    """Force a specific check to PASS (e.g. a QA reviewer overturning a false-positive fail)."""
    conn = await get_db()
    try:
        cursor = await conn.execute(
            "SELECT id, reasoning FROM evaluations WHERE lead_id = ? AND check_id = ?", (lead_id, check_id)
        )
        row = await cursor.fetchone()
        if row is None:
            return False
        new_reasoning = f"{row['reasoning'] or ''} [OVERTURNED: {reason}]".strip()
        await conn.execute(
            "INSERT INTO overrides (evaluation_id, original_status, new_status, note) "
            "VALUES (?, (SELECT status FROM evaluations WHERE id = ?), 'PASS', ?)",
            (row["id"], row["id"], reason),
        )
        await conn.execute(
            "UPDATE evaluations SET status = 'PASS', reasoning = ? WHERE id = ?",
            (new_reasoning, row["id"]),
        )
        await conn.commit()
        return True
    finally:
        await conn.close()


async def insert_override(evaluation_id: int, original_status: str, new_status: str, note: Optional[str]) -> None:
    conn = await get_db()
    try:
        await conn.execute(
            "INSERT INTO overrides (evaluation_id, original_status, new_status, note) VALUES (?, ?, ?, ?)",
            (evaluation_id, original_status, new_status, note),
        )
        await conn.execute("UPDATE evaluations SET status = ? WHERE id = ?", (new_status, evaluation_id))
        await conn.commit()
    finally:
        await conn.close()


async def list_leads() -> list[aiosqlite.Row]:
    conn = await get_db()
    try:
        cursor = await conn.execute("SELECT * FROM leads ORDER BY created_at DESC")
        return await cursor.fetchall()
    finally:
        await conn.close()


REPEAT_OFFENCE_THRESHOLD = 3


async def get_dashboard_stats(rolling_days: int = 7) -> dict:
    """Pure-SQL rollups: no LLM calls, so this costs $0 in API credits."""
    conn = await get_db()
    try:
        total_row = await (await conn.execute("SELECT COUNT(*) AS n FROM leads")).fetchone()
        total_leads = total_row["n"]

        status_rows = await (await conn.execute(
            "SELECT status, COUNT(*) AS n FROM leads GROUP BY status"
        )).fetchall()
        status_counts = {r["status"]: r["n"] for r in status_rows}

        def pct(count: int) -> float:
            return round(100 * count / total_leads, 1) if total_leads else 0.0

        first_pass_yield = pct(status_counts.get("AUTO_SUBMIT", 0))
        critical_fail_rate = pct(status_counts.get("HELD", 0))

        failing_checks_rows = await (await conn.execute(
            """SELECT check_id, COUNT(*) AS fail_count
               FROM evaluations
               WHERE status = 'FAIL'
               GROUP BY check_id
               ORDER BY fail_count DESC"""
        )).fetchall()
        failing_checks_breakdown = [dict(r) for r in failing_checks_rows]

        since = (datetime.now(timezone.utc) - timedelta(days=rolling_days)).isoformat()
        repeat_rows = await (await conn.execute(
            """SELECT l.agent_id, e.check_id, COUNT(*) AS fail_count
               FROM evaluations e
               INNER JOIN leads l ON l.id = e.lead_id
               WHERE e.status = 'FAIL' AND l.agent_id IS NOT NULL AND e.created_at >= ?
               GROUP BY l.agent_id, e.check_id
               HAVING COUNT(*) >= ?
               ORDER BY fail_count DESC""",
            (since, REPEAT_OFFENCE_THRESHOLD),
        )).fetchall()
        repeat_offenders = [dict(r) for r in repeat_rows]

        return {
            "total_leads": total_leads,
            "status_counts": status_counts,
            "first_pass_yield_pct": first_pass_yield,
            "critical_fail_rate_pct": critical_fail_rate,
            "failing_checks_breakdown": failing_checks_breakdown,
            "repeat_offenders": repeat_offenders,
            "repeat_offender_warning": len(repeat_offenders) > 0,
        }
    finally:
        await conn.close()
