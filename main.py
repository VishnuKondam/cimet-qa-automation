"""FastAPI app: ingestion, gating, traceability API, and the auditor UI."""
import asyncio
import json
import os
import re
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from deepgram import DeepgramClient
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape

import database as db
from evaluator import (
    apply_gate_logic,
    behaviour_checks,
    compute_scores,
    redact_transcript,
    rolling_window_start,
    run_llm_checks,
    should_sample_clean_call,
)
from models import LeadIngestRequest, OverrideRequest, OverturnRequest, TranscriptSegment

DEFAULT_RETAILER_ID = "retailer_1"


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.init_db()
    yield


app = FastAPI(title="CIMET QA Automation", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")
app.mount("/data", StaticFiles(directory="data"), name="data")
templates = Jinja2Templates(directory="templates")


def render_transcript_text(text: str) -> Markup:
    """Escape transcript text, then style the backend's [REDACTED_CARD_DATA] marker as a masked PCI badge."""
    badge = (
        '<span class="bg-slate-700 text-slate-400 px-1 rounded font-mono">**** **** **** ****</span>'
        '<span class="ml-1 text-[10px] uppercase tracking-wide bg-rose-500/10 text-rose-400 '
        'border border-rose-500/20 px-1.5 py-0.5 rounded-full align-middle" '
        'title="Raw card digits are redacted server-side before storage (PCI-DSS)">PCI Redacted</span>'
    )
    return Markup(str(escape(text)).replace("[REDACTED_CARD_DATA]", badge))


templates.env.filters["render_transcript_text"] = render_transcript_text

AUDIO_DIR = Path(__file__).parent / "data" / "audio"


def _slugify_lead_name(name: str) -> str:
    """Turn a user-supplied sale/lead name into a safe id fragment (lowercase, alnum + underscores)."""
    slug = re.sub(r"[^a-z0-9]+", "_", name.strip().lower()).strip("_")
    return slug[:40]


def _find_audio_url(lead_id: str) -> str | None:
    """Recordings are only saved for leads that came through the audio-upload path."""
    for ext in ("mp3", "wav"):
        if (AUDIO_DIR / f"{lead_id}.{ext}").exists():
            return f"/data/audio/{lead_id}.{ext}"
    return None

REPEAT_OFFENCE_THRESHOLD = 3


@app.post("/api/leads/{lead_id}/process")
async def process_lead(lead_id: str, payload: LeadIngestRequest):
    call_date = payload.call_ts[:10]

    cleaned_segments, card_violation = redact_transcript(payload.transcript)
    transcript_dicts = [s.model_dump() for s in cleaned_segments]

    await db.insert_lead(lead_id, payload.retailer_id, payload.agent_id, payload.call_ts, payload.crm_payload)
    await db.insert_transcript_segments(lead_id, transcript_dicts)

    active_checks = [dict(r) for r in await db.get_active_checks(payload.retailer_id, call_date)]
    checks_by_id = {c["id"]: c for c in active_checks}
    behaviour_ids = [c["id"] for c in active_checks if c["check_type"] == "behaviour"]
    llm_checks = [c for c in active_checks if c["check_type"] != "behaviour"]

    behaviour_results = behaviour_checks(transcript_dicts, behaviour_ids)
    llm_results = await run_llm_checks(llm_checks, transcript_dicts, payload.crm_payload)

    evaluations = []
    for r in [*behaviour_results, *llm_results]:
        check = checks_by_id.get(r.check_id)
        evaluations.append({
            "check_id": r.check_id,
            "check_version_effective_from": check["effective_from"] if check else call_date,
            "status": r.status,
            "transcript_quote": r.transcript_quote,
            "timestamp": r.audio_timestamp_start,
            "expected_value": r.expected_value,
            "reasoning": r.reasoning,
        })
    await db.insert_evaluations(lead_id, evaluations)

    status = apply_gate_logic(evaluations, checks_by_id)

    sampled_clean = False
    if status == "AUTO_SUBMIT":
        sampled_clean = should_sample_clean_call(lead_id)
        if sampled_clean:
            status = "QA"  # 5% of clean calls still routed to a human, we're measuring the model

    await db.update_lead_status(lead_id, status, card_violation, sampled_clean)

    score_with_fatal, score_without_fatal = compute_scores(evaluations, checks_by_id)
    await db.upsert_scores(lead_id, score_with_fatal, score_without_fatal)

    if payload.agent_id:
        since = rolling_window_start(days=7)
        for ev in evaluations:
            if ev["status"] != "FAIL":
                continue
            check = checks_by_id.get(ev["check_id"])
            if not check or not check["is_critical"]:
                continue
            fail_count = await db.count_recent_critical_fails(payload.agent_id, ev["check_id"], since)
            if fail_count >= REPEAT_OFFENCE_THRESHOLD:
                evaluations_repeat_note = (
                    f"Repeat offence: agent {payload.agent_id} has failed check "
                    f"'{ev['check_id']}' {fail_count} times in the last 7 days."
                )
                ev["reasoning"] = f"{ev['reasoning']} | {evaluations_repeat_note}"

    return {
        "lead_id": lead_id,
        "status": status,
        "card_data_violation": card_violation,
        "sampled_clean": sampled_clean,
        "score_with_fatal": score_with_fatal,
        "score_without_fatal": score_without_fatal,
        "evaluations": evaluations,
    }


@app.get("/api/leads/{lead_id}")
async def get_lead(lead_id: str):
    bundle = await db.get_lead_bundle(lead_id)
    if bundle is None:
        raise HTTPException(status_code=404, detail="Lead not found")
    return bundle


@app.post("/api/evaluations/{evaluation_id}/override")
async def override_evaluation(evaluation_id: int, payload: OverrideRequest):
    existing = await db.get_evaluation(evaluation_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="Evaluation not found")
    await db.insert_override(evaluation_id, existing["status"], payload.new_status, payload.note)
    return {"evaluation_id": evaluation_id, "original_status": existing["status"], "new_status": payload.new_status}


@app.post("/api/leads/{lead_id}/overturn/{check_id}")
async def overturn_check(lead_id: str, check_id: str, payload: OverturnRequest):
    lead = await db.get_lead(lead_id)
    if lead is None:
        raise HTTPException(status_code=404, detail="Lead not found")

    updated = await db.overturn_evaluation(lead_id, check_id, payload.reason)
    if not updated:
        raise HTTPException(status_code=404, detail="Evaluation not found for this lead/check")

    call_date = lead["call_ts"][:10]
    active_checks = [dict(r) for r in await db.get_active_checks(lead["retailer_id"], call_date)]
    checks_by_id = {c["id"]: c for c in active_checks}
    evaluations = [dict(r) for r in await db.get_evaluations_for_lead(lead_id)]

    status = apply_gate_logic(evaluations, checks_by_id)
    score_with_fatal, score_without_fatal = compute_scores(evaluations, checks_by_id)

    await db.update_lead_status(lead_id, status, bool(lead["card_data_violation"]), bool(lead["sampled_clean"]))
    await db.upsert_scores(lead_id, score_with_fatal, score_without_fatal)

    return {
        "lead_id": lead_id,
        "check_id": check_id,
        "status": status,
        "score_with_fatal": score_with_fatal,
        "score_without_fatal": score_without_fatal,
    }


@app.get("/api/dashboard/stats")
async def dashboard_stats():
    return await db.get_dashboard_stats()


@app.post("/api/evaluate-audio")
async def evaluate_audio(
    file: UploadFile = File(...),
    crm_payload: str = Form("{}"),
    lead_name: str = Form(""),
    stt_model: str = Form("nova-3"),
    use_llm_fallback: str = Form("true"),
):
    """Demo-only path: transcribe an uploaded call recording via Deepgram, then run it
    through the same dynamic check-library evaluator used by /api/leads/{id}/process.
    """
    api_key = os.environ.get("DEEPGRAM_API_KEY")
    if not api_key:
        raise HTTPException(status_code=503, detail="DEEPGRAM_API_KEY not configured")

    stt_model = stt_model if stt_model in ("nova-3", "nova-2") else "nova-3"
    use_llm = use_llm_fallback.strip().lower() != "false"

    try:
        crm_payload_dict = json.loads(crm_payload) if crm_payload else {}
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="crm_payload must be valid JSON")

    audio_bytes = await file.read()
    client = DeepgramClient(api_key=api_key)
    try:
        response = await asyncio.to_thread(
            client.listen.v1.media.transcribe_file,
            request=audio_bytes,
            model=stt_model,
            smart_format=False,
            diarize=True,
            utterances=True,
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Deepgram transcription failed: {exc}")

    utterances = response.results.utterances or [] if response.results else []
    if not utterances:
        return {
            "status": "QA",
            "evaluations": [],
            "reasoning": "Deepgram returned no utterances for this recording; routed to human QA.",
        }

    raw_segments = [
        {
            "speaker": f"speaker_{u.speaker}",
            "text": u.transcript,
            "start_ts": u.start,
            "end_ts": u.end,
        }
        for u in utterances
    ]
    cleaned_segments, card_violation = redact_transcript([TranscriptSegment(**s) for s in raw_segments])
    transcript_dicts = [s.model_dump() for s in cleaned_segments]

    call_date = datetime.utcnow().strftime("%Y-%m-%d")
    active_checks = [dict(r) for r in await db.get_active_checks(DEFAULT_RETAILER_ID, call_date)]
    checks_by_id = {c["id"]: c for c in active_checks}
    behaviour_ids = [c["id"] for c in active_checks if c["check_type"] == "behaviour"]
    llm_checks = [c for c in active_checks if c["check_type"] != "behaviour"]

    behaviour_results = behaviour_checks(transcript_dicts, behaviour_ids)
    llm_results = await run_llm_checks(llm_checks, transcript_dicts, crm_payload_dict, use_llm=use_llm)

    evaluations = []
    for r in [*behaviour_results, *llm_results]:
        check = checks_by_id.get(r.check_id)
        evaluations.append({
            "check_id": r.check_id,
            "check_version_effective_from": check["effective_from"] if check else call_date,
            "status": r.status,
            "transcript_quote": r.transcript_quote,
            "timestamp": r.audio_timestamp_start,
            "expected_value": r.expected_value,
            "reasoning": r.reasoning,
        })

    status = apply_gate_logic(evaluations, checks_by_id)
    score_with_fatal, score_without_fatal = compute_scores(evaluations, checks_by_id)

    lead_id = f"audio_{uuid4().hex[:10]}"
    slug = _slugify_lead_name(lead_name)
    if slug:
        candidate = slug
        if await db.get_lead(candidate) is not None:
            candidate = f"{slug}_{uuid4().hex[:6]}"
        lead_id = candidate
    call_ts = datetime.utcnow().isoformat()

    audio_ext = Path(file.filename).suffix.lstrip(".").lower() if file.filename else ""
    if audio_ext not in ("mp3", "wav"):
        audio_ext = "wav"
    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    (AUDIO_DIR / f"{lead_id}.{audio_ext}").write_bytes(audio_bytes)

    await db.insert_lead(lead_id, DEFAULT_RETAILER_ID, None, call_ts, crm_payload_dict)
    await db.insert_transcript_segments(lead_id, transcript_dicts)
    await db.insert_evaluations(lead_id, evaluations)
    await db.update_lead_status(lead_id, status, card_violation, False)
    await db.upsert_scores(lead_id, score_with_fatal, score_without_fatal)

    return {
        "lead_id": lead_id,
        "status": status,
        "card_data_violation": card_violation,
        "score_with_fatal": score_with_fatal,
        "score_without_fatal": score_without_fatal,
        "evaluations": evaluations,
        "transcript": transcript_dicts,
    }


@app.get("/")
async def index():
    leads = await db.list_leads()
    if not leads:
        return RedirectResponse(url="/leads")
    return RedirectResponse(url=f"/leads/{leads[0]['id']}")


@app.get("/leads")
async def list_leads_page(request: Request):
    leads = await db.list_leads()
    stats = await db.get_dashboard_stats()
    return templates.TemplateResponse(request, "leads.html", {"leads": leads, "stats": stats})


@app.get("/leads/{lead_id}")
async def lead_detail_page(request: Request, lead_id: str):
    bundle = await db.get_lead_bundle(lead_id)
    if bundle is None:
        raise HTTPException(status_code=404, detail="Lead not found")
    audio_url = _find_audio_url(lead_id)
    return templates.TemplateResponse(request, "index.html", {"lead_id": lead_id, "bundle": bundle, "audio_url": audio_url})


@app.get("/evaluate")
async def evaluate_audio_page(request: Request):
    return templates.TemplateResponse(request, "evaluate.html", {})
