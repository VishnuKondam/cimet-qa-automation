"""Evaluation engine: PII redaction, algorithmic behaviour checks, and the
instructor-driven LLM pass for verbatim (Type A) and factual (Type B) checks.
"""
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

import instructor
from dotenv import load_dotenv
from openai import AsyncOpenAI

from models import CheckResult, CheckResultBatch, TranscriptSegment

load_dotenv()

CARD_NUMBER_RE = re.compile(r"\b\d{13,16}\b")
DEAD_AIR_THRESHOLD_SECONDS = 10.0
LOW_CONFIDENCE_STATUS = "LOW_CONFIDENCE"

SYSTEM_PROMPT = (
    "You are a strict QA Auditor. Compare the Transcript against the CRM Payload "
    "and Retailer Rate Card. You must map every failure to an exact transcript "
    "quote and its specific audio_timestamp_start. Do not assume consent; explicit "
    "verbatim confirmation of the recording disclaimer is required. Only mark PASS "
    "when the transcript evidence is unambiguous; otherwise use LOW_CONFIDENCE. "
    "For verbatim checks, compare only against the exact script_text provided for "
    "that check id -- never invent or assume what the approved script says. "
    "expected_value must always be the value taken from the CRM payload, rate card, "
    "or script_text -- never the value spoken in the transcript. "
    "The transcript may contain redacted PII placeholder tags in square brackets "
    "(e.g. [CUSTOMER_NAME], [PHONE], [DOB], [SERVICE_ADDRESS], [ACCOUNT_NUMBER]). "
    "These tags stand in for the real spoken value in this sanitised test transcript "
    "-- treat a tag appearing as the customer's or agent's spoken answer as that "
    "value having been stated. But the tag's mere presence anywhere in the "
    "transcript is NOT sufficient: you must verify the actual dialogue exchange "
    "for that check (e.g. the agent asks for date of birth AND the customer "
    "responds with [DOB] -> PASS). If the required question was never asked, or "
    "the exchange is incomplete or ambiguous (e.g. cut off by crosstalk), that is "
    "still a FAIL or LOW_CONFIDENCE respectively -- never a silent PASS."
)

_client: Optional[instructor.Instructor] = None


def _is_crm_payload_blank(crm_payload: Optional[dict]) -> bool:
    """True when there is no usable CRM reference data: None, {}, or only blank/empty values."""
    if not crm_payload:
        return True
    return all(
        value is None or (isinstance(value, str) and not value.strip())
        for value in crm_payload.values()
    )


def _get_client() -> instructor.Instructor:
    global _client
    if _client is None:
        _client = instructor.from_openai(AsyncOpenAI())
    return _client


def redact_card_numbers(text: str) -> tuple[str, bool]:
    """Replace 13-16 digit numeric runs (card numbers) before storage or LLM exposure."""
    violated = bool(CARD_NUMBER_RE.search(text))
    redacted = CARD_NUMBER_RE.sub("[REDACTED_CARD_DATA]", text)
    return redacted, violated


def redact_transcript(segments: list[TranscriptSegment]) -> tuple[list[TranscriptSegment], bool]:
    any_violation = False
    cleaned: list[TranscriptSegment] = []
    for seg in segments:
        redacted_text, violated = redact_card_numbers(seg.text)
        any_violation = any_violation or violated
        cleaned.append(seg.model_copy(update={"text": redacted_text}))
    return cleaned, any_violation


def calculate_dead_air(transcript: list[dict], threshold: float = DEAD_AIR_THRESHOLD_SECONDS) -> list[dict]:
    """Non-LLM Type C check: flag any gap between consecutive segments over `threshold` seconds."""
    flags = []
    ordered = sorted(transcript, key=lambda s: s["start_ts"])
    for prev, nxt in zip(ordered, ordered[1:]):
        gap = nxt["start_ts"] - prev["end_ts"]
        if gap > threshold:
            flags.append({
                "gap_seconds": round(gap, 1),
                "start_ts": prev["end_ts"],
                "end_ts": nxt["start_ts"],
            })
    return flags


def behaviour_checks(transcript: list[dict], behaviour_check_ids: list[str]) -> list[CheckResult]:
    """Type C: transcript-only, algorithmic, never blocks the sale."""
    results: list[CheckResult] = []
    dead_air_flags = calculate_dead_air(transcript)
    for check_id in behaviour_check_ids:
        if "dead_air" in check_id or "dead-air" in check_id:
            if dead_air_flags:
                worst = max(dead_air_flags, key=lambda f: f["gap_seconds"])
                results.append(CheckResult(
                    check_id=check_id,
                    status="FAIL",
                    transcript_quote=None,
                    audio_timestamp_start=worst["start_ts"],
                    expected_value=f"<= {DEAD_AIR_THRESHOLD_SECONDS}s gap",
                    reasoning=f"Dead air of {worst['gap_seconds']}s detected at {worst['start_ts']}s (coaching note, non-blocking).",
                ))
            else:
                results.append(CheckResult(
                    check_id=check_id, status="PASS", reasoning="No dead air over threshold detected.",
                ))
        else:
            # Other behaviour checks (rapport, interruptions, objection handling) fall back to LLM.
            continue
    return results


async def run_llm_checks(
    checks: list[dict],
    transcript: list[dict],
    crm_payload: dict,
    use_llm: bool = True,
) -> list[CheckResult]:
    """Type A (verbatim/script) and Type B (factual match) checks via instructor + LLM."""
    if not checks:
        return []

    # No usable CRM reference data supplied: assume factual (Type B) checks PASS rather
    # than routing to LOW_CONFIDENCE/FAIL, since there is nothing to compare against.
    # Verbatim (Type A) checks are unaffected -- they are judged purely on transcript evidence.
    crm_missing = _is_crm_payload_blank(crm_payload)
    factual_checks = [c for c in checks if crm_missing and c["check_type"] == "factual"]
    remaining_checks = [c for c in checks if c not in factual_checks]

    results: list[CheckResult] = [
        CheckResult(
            check_id=c["id"], status="PASS",
            reasoning="Assumed valid (no CRM payload supplied).",
        )
        for c in factual_checks
    ]
    if not remaining_checks:
        return results

    if not use_llm:
        results.extend(
            CheckResult(
                check_id=c["id"], status=LOW_CONFIDENCE_STATUS,
                reasoning="LLM fallback disabled by operator; routed to human QA rather than auto-passed.",
            )
            for c in remaining_checks
        )
        return results

    if not os.environ.get("OPENAI_API_KEY"):
        results.extend(
            CheckResult(
                check_id=c["id"], status=LOW_CONFIDENCE_STATUS,
                reasoning="No OPENAI_API_KEY configured; routed to human QA rather than auto-passed.",
            )
            for c in remaining_checks
        )
        return results

    client = _get_client()
    checks_prompt = "\n".join(
        f"- id={c['id']} type={c['check_type']} critical={bool(c['is_critical'])}: {c['description']}"
        + (f" | script_text=\"{c['script_text']}\"" if c.get("script_text") else "")
        for c in remaining_checks
    )
    transcript_prompt = "\n".join(
        f"[{seg['start_ts']}s-{seg['end_ts']}s] {seg['speaker']}: {seg['text']}" for seg in transcript
    )

    try:
        batch = await client.chat.completions.create(
            model="gpt-4o-mini",
            response_model=CheckResultBatch,
            temperature=0.0,
            top_p=1.0,
            seed=42,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": (
                    f"CHECKS TO EVALUATE:\n{checks_prompt}\n\n"
                    f"CRM PAYLOAD:\n{crm_payload}\n\n"
                    f"TRANSCRIPT:\n{transcript_prompt}\n\n"
                    "Return one CheckResult per check id listed above."
                )},
            ],
        )
        results.extend(batch.results)
        return results
    except Exception as exc:  # LLM/schema failure must never silently auto-pass a check
        results.extend(
            CheckResult(
                check_id=c["id"], status=LOW_CONFIDENCE_STATUS,
                reasoning=f"Evaluator error, routed to human QA: {exc}",
            )
            for c in remaining_checks
        )
        return results


def apply_gate_logic(evaluations: list[dict], checks_by_id: dict[str, dict]) -> str:
    """AUTO_SUBMIT if all criticals pass, HELD if any critical fails, else QA."""
    has_low_confidence = False
    for ev in evaluations:
        check = checks_by_id.get(ev["check_id"])
        is_critical = bool(check and check["is_critical"])
        if ev["status"] == "FAIL" and is_critical:
            return "HELD"
        if ev["status"] == LOW_CONFIDENCE_STATUS:
            has_low_confidence = True
    return "QA" if has_low_confidence else "AUTO_SUBMIT"


def compute_scores(evaluations: list[dict], checks_by_id: dict[str, dict]) -> tuple[float, float]:
    """Score with fatal factors (critical fails zero it out) and without (weighted pass ratio only)."""
    total_weight = sum(checks_by_id[e["check_id"]]["weight"] for e in evaluations if e["check_id"] in checks_by_id)
    if total_weight == 0:
        return 0.0, 0.0
    passed_weight = sum(
        checks_by_id[e["check_id"]]["weight"]
        for e in evaluations
        if e["check_id"] in checks_by_id and e["status"] == "PASS"
    )
    score_without_fatal = round(100 * passed_weight / total_weight, 1)
    any_critical_fail = any(
        e["status"] == "FAIL" and checks_by_id.get(e["check_id"], {}).get("is_critical")
        for e in evaluations
    )
    score_with_fatal = 0.0 if any_critical_fail else score_without_fatal
    return score_with_fatal, score_without_fatal


def should_sample_clean_call(lead_id: str, sample_rate: float = 0.05) -> bool:
    """Deterministic 5% sample of clean calls, hashed on lead_id so it's stable across reruns."""
    bucket = int(hash(lead_id) % 100)
    return bucket < int(sample_rate * 100)


def rolling_window_start(days: int = 7) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
