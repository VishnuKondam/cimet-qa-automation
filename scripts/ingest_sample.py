"""Sends the sample lead payload to the running server for an end-to-end smoke test:
    /usr/local/bin/python3 scripts/ingest_sample.py
"""
import json
import sys
import urllib.request
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
LEAD_ID = "3613790"
URL = f"http://127.0.0.1:8000/api/leads/{LEAD_ID}/process"


def main():
    payload = json.loads((DATA_DIR / "sample_lead_3613790.json").read_text())
    req = urllib.request.Request(
        URL, data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req) as resp:
        result = json.loads(resp.read())
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"Ingestion failed: {exc}", file=sys.stderr)
        sys.exit(1)
