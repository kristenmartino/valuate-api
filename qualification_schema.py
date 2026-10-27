"""Machine-readable publication evidence; extraction confidence is separate."""

from typing import Literal
from pydantic import BaseModel, Field


class InputIssue(BaseModel):
    code: str
    field_path: str
    period_end: str | None = None
    message: str


class InputEvidence(BaseModel):
    field_path: str
    period_end: str
    value: str
    unit: str
    scope: str
    basis: str
    accession: str
    start: str | None = None
    concepts: list[str] = Field(default_factory=list)
    operands: list[dict[str, str]] = Field(default_factory=list)
    dimensions: dict[str, str] = Field(default_factory=dict)


class Qualification(BaseModel):
    status: Literal["qualified", "review_required", "unsupported"]
    policy_version: str
    record_digest: str
    filing_accession: str
    checked_at: str | None
    issues: list[InputIssue] = Field(default_factory=list)
    disclosures: list[str] = Field(default_factory=list)
    inputs: list[InputEvidence] = Field(default_factory=list)
    evidence_digest: str | None = None
    latest_filing_checked: str | None = None
