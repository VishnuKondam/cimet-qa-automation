"""Live network smoke test for /api/evaluate-audio. Requires the server running
(see run instructions below) and a real DEEPGRAM_API_KEY + OPENAI_API_KEY in .env.

    /usr/local/bin/python3 -m uvicorn main:app --port 8010 &
    /usr/local/bin/python3 test_audio_endpoint.py
"""
import json

import requests

BASE_URL = "http://127.0.0.1:8010"
AUDIO_PATH = "/tmp/sample_call.wav"

MOCK_CRM_PAYLOAD = {
    "lead_id": "test-audio-001",
    "account_holder_name": "John Smith",
    "email": "j.smith@gmail.com",
    "plan_peak_rate_c_per_kwh": 31.9,
    "fuel_type": "electricity",
}


def main():
    with open(AUDIO_PATH, "rb") as f:
        files = {"file": ("sample_call.wav", f, "audio/wav")}
        data = {"crm_payload": json.dumps(MOCK_CRM_PAYLOAD)}
        resp = requests.post(f"{BASE_URL}/api/evaluate-audio", files=files, data=data, timeout=60)

    print(f"Status: {resp.status_code}")
    print(json.dumps(resp.json(), indent=2))


if __name__ == "__main__":
    main()
