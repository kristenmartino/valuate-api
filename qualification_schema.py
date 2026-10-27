"""Machine-readable publication evidence; extraction confidence is separate."""

from typing import Literal
from pydantic import BaseModel, Field, model_serializer


class ValueScope(BaseModel):
    kind: Literal["parent_equity_proxy"] = "parent_equity_proxy"
    common_share_valuation_verified: Literal[False] = False
    preferred_claims_deducted: Literal[False] = False
    share_basis: str
    description: str


class StatementWarning(BaseModel):
    code: Literal["historical_balance_unreconciled", "historical_ownership_incomplete"]
    fiscal_year: int
    period_end: str
    message: str
    difference: str
    field_paths: list[str]
    source_accessions: dict[str, str] = Field(default_factory=dict)


class InputIssue(BaseModel):
    code: str
    field_path: str
    period_end: str | None = None
    message: str


class InputEvidence(BaseModel):
    field_path: str
    period_end: str
    display_label: str | None = None
    is_proxy: bool = False
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
    method: str = "reviewed_filing_contract"
    ruleset: str | None = None
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
    value_scope: ValueScope | None = None
    statement_warnings: list[StatementWarning] = Field(default_factory=list)

    @model_serializer(mode="wrap")
    def _serialize(self, handler):
        data = handler(self)
        # Preserve the stored payload/CAS identity of old qualifications until
        # they are explicitly refreshed. New metadata never changes financials.
        if self.value_scope is None:
            data.pop("value_scope", None)
        if not self.statement_warnings:
            data.pop("statement_warnings", None)
        return data
