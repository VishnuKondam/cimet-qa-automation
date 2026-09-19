"""Pydantic schemas shared by the ingestion API and the instructor-driven evaluator."""
from typing import Literal, Optional

from pydantic import BaseModel, Field


class TranscriptSegment(BaseModel):
    speaker: str
    text: str
    start_ts: float
    end_ts: float


class CRMPayload(BaseModel):
    """Loosely typed passthrough of whatever fields the retailer's CRM record carries."""
    model_config = {"extra": "allow"}


class LeadIngestRequest(BaseModel):
    retailer_id: str
    agent_id: Optional[str] = None
    call_ts: str = Field(description="ISO timestamp of the call, used to resolve check-library version")
    crm_payload: dict
    transcript: list[TranscriptSegment]


class CheckResult(BaseModel):
    check_id: str
    status: Literal["PASS", "FAIL", "LOW_CONFIDENCE"]
    transcript_quote: Optional[str] = None
    audio_timestamp_start: Optional[float] = None
    expected_value: Optional[str] = None
    reasoning: str


class CheckResultBatch(BaseModel):
    results: list[CheckResult]


class OverrideRequest(BaseModel):
    new_status: Literal["PASS", "FAIL", "LOW_CONFIDENCE"]
    note: Optional[str] = None


class OverturnRequest(BaseModel):
    reason: str
