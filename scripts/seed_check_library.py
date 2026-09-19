"""One-off seed script: loads a retailer's check-library export into SQLite. Run with:
    /usr/local/bin/python3 scripts/seed_check_library.py
"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import database as db

DATA_DIR = Path(__file__).resolve().parent.parent / "data"


async def main():
    await db.init_db()
    checks = json.loads((DATA_DIR / "check_library_retailer_1.json").read_text())
    await db.seed_check_library("retailer_1", checks)
    print(f"Seeded {len(checks)} checks for retailer_1")


if __name__ == "__main__":
    asyncio.run(main())
