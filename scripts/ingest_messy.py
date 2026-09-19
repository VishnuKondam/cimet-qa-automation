"""Stress-test the LLM evaluator against a messy, adversarial transcript:
    /usr/local/bin/python3 scripts/ingest_messy.py
Prints the raw evaluations array so FACT_RATES_CHARGES / email mismatch can be verified by eye.
"""
import json
import sys
import urllib.request
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
LEAD_ID = "9900001"
URL = f"http://127.0.0.1:8000/api/leads/{LEAD_ID}/process"


def main():
    payload = json.loads((DATA_DIR / "sample_messy_lead.json").read_text())
    req = urllib.request.Request(
        URL, data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req) as resp:
        result = json.loads(resp.read())
    print(json.dumps(result["evaluations"], indent=2))
    print(f"\nGate status: {result['status']}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"Ingestion failed: {exc}", file=sys.stderr)
        sys.exit(1)
