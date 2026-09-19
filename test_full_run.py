"""Standalone smoke test: seed the check library, run the full evaluator pipeline
against the parsed PDF worked example, and report timing + token usage.

    /usr/local/bin/python3 test_full_run.py
"""
import asyncio
import json
import time
from pathlib import Path

import database as db
import evaluator

DATA_DIR = Path(__file__).resolve().parent / "data"


async def main():
    await db.init_db()
    checks = json.loads((DATA_DIR / "check_library_retailer_1.json").read_text())
    await db.seed_check_library("retailer_1", checks)

    lead = json.loads((DATA_DIR / "sample_lead_3613790.json").read_text())
    call_date = lead["call_ts"][:10]
    active_checks = [dict(row) for row in await db.get_active_checks("retailer_1", call_date)]

    non_llm_ids = {"dead_air"}
    llm_checks = [c for c in active_checks if c["id"] not in non_llm_ids]

    start = time.perf_counter()
    results = await evaluator.run_llm_checks(llm_checks, lead["transcript"], lead["crm_payload"])
    elapsed = time.perf_counter() - start

    checks_by_id = {c["id"]: c for c in active_checks}
    score_with_fatal, score_without_fatal = evaluator.compute_scores(
        [{"check_id": r.check_id, "status": r.status} for r in results], checks_by_id
    )
    gate = evaluator.apply_gate_logic(
        [{"check_id": r.check_id, "status": r.status} for r in results], checks_by_id
    )

    print(json.dumps([r.model_dump() for r in results], indent=2))
    print(f"\nGate decision: {gate}")
    print(f"Score (with fatal factors): {score_with_fatal}")
    print(f"Score (without fatal factors): {score_without_fatal}")
    print(f"Execution time: {elapsed:.2f}s")

    disclaimer = next((r for r in results if r.check_id == "recording_disclaimer"), None)
    print(f"\nrecording_disclaimer -> {disclaimer.status if disclaimer else 'MISSING'}")


if __name__ == "__main__":
    asyncio.run(main())
