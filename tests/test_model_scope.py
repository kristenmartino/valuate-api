"""Future-issuer ownership and historical-display safeguards, not approvals."""
import asyncio
import copy
from decimal import Decimal
from functools import lru_cache
import pytest
from pydantic import ValidationError

from eval.cold import FixtureEdgar
from eval.reusable_cold import run_fixture, ReusableEdgar
from model_scope import value_scope, parent_model_issues, historical_statement_warnings
from qualification import current_qualification
from qualification_schema import InputEvidence, Qualification, ValueScope
from schemas import LineItem, ExtractionSource


def line(value, tag=None):
    return LineItem(value=Decimal(value), source=ExtractionSource.XBRL, confidence=1, xbrl_tag=tag)


def future():
    c = FixtureEdgar('PRU').saved.model_copy(deep=True)
    return c.model_copy(update={'ticker': 'FUTURE', 'cik': '0000000123', 'name': 'Synthetic future issuer'})


@lru_cache
def met():
    return asyncio.run(run_fixture('MET'))[0]


def test_future_parent_model_cannot_claim_verified_common_stock_value():
    c = future();before = c.model_dump(mode='json')
    scope = value_scope(c)
    assert scope.kind == 'parent_equity_proxy'
    assert scope.common_share_valuation_verified is False and scope.preferred_claims_deducted is False
    assert 'Preferred capital' in scope.description and 'par value' in scope.description
    assert not parent_model_issues(c)
    assert c.model_dump(mode='json') == before


@pytest.mark.parametrize('field', ['common_share_valuation_verified', 'preferred_claims_deducted'])
def test_unsupported_common_ownership_claims_are_rejected_by_the_schema(field):
    data = value_scope(future()).model_dump();data[field] = True
    with pytest.raises(ValidationError): ValueScope.model_validate(data)


@pytest.mark.parametrize('path,tag', [
    ('equity', 'us-gaap:StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest'),
    ('income', 'us-gaap:ProfitLoss'), ('equity', None), ('income', None),
])
def test_parent_proxy_still_requires_parent_not_inclusive_or_unknown_scope(path, tag):
    c = future();p = c.periods[1]
    item = p.balance_sheet.shareholders_equity if path == 'equity' else p.income_statement.net_income
    item.xbrl_tag = tag
    issues = parent_model_issues(c)
    assert len(issues) == 1 and issues[0].code == 'parent_proxy_scope_unverified'


def test_non_pb_model_does_not_gain_parent_ownership_requirements():
    c = FixtureEdgar('AAPL').saved
    assert value_scope(c) is None and parent_model_issues(c) == []


def test_met_zero_preferred_par_never_proves_zero_preferred_capital():
    c = met();q = c.qualification
    rows = ReusableEdgar('MET').facts['facts']['us-gaap']['PreferredStockValue']['units']['USD']
    assert any(r['accn'] == c.periods[0].filing_accession and r['end'] == '2025-12-31' and r['val'] == 0 for r in rows)
    assert q.status == 'qualified' and q.value_scope.kind == 'parent_equity_proxy'
    assert q.value_scope.common_share_valuation_verified is False
    assert c.periods[0].balance_sheet.shareholders_equity.value == Decimal('28398000000')
    assert c.periods[0].income_statement.net_income.value == Decimal('3379000000')


def test_met_historical_warning_reports_exact_gap_and_source_bases_without_repairing_values():
    c = met();before = c.model_dump(mode='json');q = c.qualification
    assert len(q.statement_warnings) == 1
    w = q.statement_warnings[0]
    assert w.fiscal_year == 2021 and w.difference == '17736000000'
    assert w.code == 'historical_balance_unreconciled'
    assert w.source_accessions['balance_sheet.total_assets'] == '0001099219-22-000014'
    assert w.source_accessions['balance_sheet.shareholders_equity'] == '0001099219-24-000035'
    assert historical_statement_warnings(c, q.inputs) == q.statement_warnings
    assert c.model_dump(mode='json') == before
    assert c.periods[-1].balance_sheet.total_assets.value == Decimal('759708000000')
    assert c.periods[-1].balance_sheet.shareholders_equity.value == Decimal('49746000000')


@pytest.mark.parametrize('case', ['balanced_parent', 'balanced_inclusive', 'missing_nci', 'unbalanced', 'rounding'])
def test_future_historical_diagnostics_distinguish_missing_nci_from_a_known_gap(case):
    c = future();p = c.periods[1];c.periods = [c.periods[0], p];b = p.balance_sheet
    b.total_assets = line(1000);b.total_liabilities = line(800)
    b.shareholders_equity = line(190, 'us-gaap:StockholdersEquity');b.noncontrolling_interests = line(10)
    if case == 'balanced_inclusive': b.shareholders_equity = line(200, 'us-gaap:StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest')
    if case == 'missing_nci': b.shareholders_equity = line(100, 'us-gaap:StockholdersEquity');b.noncontrolling_interests = None
    if case == 'unbalanced': b.shareholders_equity.value = Decimal(100)
    if case == 'rounding': b.total_assets.value += 1
    before = c.model_dump(mode='json');warnings = historical_statement_warnings(c, [])
    if case in {'balanced_parent', 'balanced_inclusive', 'rounding'}: assert warnings == []
    elif case == 'missing_nci':
        assert warnings[0].code == 'historical_ownership_incomplete'
        assert 'not assumed to be zero' in warnings[0].message
    else: assert warnings[0].difference == '90' and warnings[0].code == 'historical_balance_unreconciled'
    assert c.model_dump(mode='json') == before


def test_latest_period_is_not_downgraded_to_a_historical_display_warning():
    from graph import validate_company
    c = met().model_copy(deep=True);c.periods[0].balance_sheet.total_assets.value += Decimal('10000000000')
    checked = validate_company(c)
    assert any(f.field_path == 'balance_sheet' for f in checked.extraction_flags)
    assert all(w.fiscal_year != c.periods[0].fiscal_year for w in historical_statement_warnings(c, c.qualification.inputs))
    assert current_qualification(checked).status == 'review_required'


@pytest.mark.parametrize('change', ['missing', 'common_claim', 'deduction_claim', 'unknown_kind'])
def test_missing_or_tampered_ownership_metadata_cannot_grant_public_calculation(monkeypatch, change):
    from app import main as api
    from repository import InMemoryRepo
    from fastapi.testclient import TestClient
    from dcf import default_assumptions
    c = met().model_copy(deep=True)
    if change == 'missing': c.qualification.value_scope = None
    else:
        key, value = {'common_claim':('common_share_valuation_verified', True),
                      'deduction_claim':('preferred_claims_deducted', True),
                      'unknown_kind':('kind', 'common_price')}[change]
        # Even an unsafe internal model_copy cannot approve a broader scope.
        c.qualification.value_scope = c.qualification.value_scope.model_copy(update={key:value})
    assert current_qualification(c).status == 'review_required'
    repo = InMemoryRepo();asyncio.run(repo.set('MET', c));monkeypatch.setattr(api, '_repo', repo)
    monkeypatch.setattr(api, '_company_locks', {})
    def forbidden(*args, **kwargs):raise AssertionError('Missing ownership scope reached model calculation')
    monkeypatch.setattr(api, 'compute_projection', forbidden)
    client = TestClient(api.app)
    assert client.get('/value/MET/defaults').status_code == 422
    assert client.post('/value/MET', json={'assumptions':default_assumptions(c).model_dump()}).status_code == 422


def test_public_api_reports_parent_proxy_even_if_caller_requests_common_value(monkeypatch):
    from app import main as api
    from repository import InMemoryRepo
    from fastapi.testclient import TestClient
    from dcf import default_assumptions
    c = met();repo = InMemoryRepo();asyncio.run(repo.set('MET', c));monkeypatch.setattr(api, '_repo', repo);monkeypatch.setattr(api, '_company_locks', {})
    client = TestClient(api.app);response = client.post('/value/MET', json={'assumptions':default_assumptions(c).model_dump(), 'value_kind':'common_stock_price', 'value_scope':{'common_share_valuation_verified':True}})
    assert response.status_code == 200
    body = response.json();assert body['value_kind'] == 'parent_equity_proxy'
    assert body['value_scope']['common_share_valuation_verified'] is False
    assert body['projection']['fair_value_per_share'] == pytest.approx(69.93898613865626)


def test_old_qualification_payload_round_trip_preserves_storage_identity():
    old = {'status':'review_required','method':'reviewed_filing_contract','ruleset':None,'policy_version':'old','record_digest':'digest','filing_accession':'a','checked_at':None,'issues':[],'disclosures':[],'inputs':[],'evidence_digest':None,'latest_filing_checked':None}
    assert Qualification.model_validate(old).model_dump(mode='json') == old
