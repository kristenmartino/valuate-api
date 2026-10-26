"""Filing-backed IBM proxy selection and a successful no-LLM extraction path."""
import asyncio
import copy
import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dcf import compute_projection, default_assumptions
from graph import _compose_company, _make_track_b, track_a, validate_company, CompositionError
from ibm import CONCEPTS, filing_items, model_items
from industry import Industry
from overrides import apply_override
from repository import InMemoryRepo
from schemas import Company

FACTS = json.loads((Path(__file__).parent / 'fixtures/ibm-annual-facts.json').read_text())
LATEST = date(2025, 12, 31)
ACCESSION = '0000051143-26-000010'


def state(facts=None):
    return {'ticker': 'IBM', 'cik': '0000051143', 'company_name': 'IBM', 'facts': facts or FACTS,
            'period_end': LATEST, 'filing_accession': ACCESSION, 'filing_url': 'https://example.test/ibm.htm', 'industry': Industry.STANDARD}


def company():
    s = state();s.update(asyncio.run(track_a(s)))
    return validate_company(_compose_company(s['ticker'], s['cik'], s['company_name'], s['period_ends'], ACCESSION, s['periods_items']))


def test_ibm_proxy_uses_same_filing_gaap_values_and_keeps_a_review_flag():
    c = company()
    assert [p.income_statement.operating_income.value for p in c.periods] == [12_263_000_000, 7_509_000_000, 10_297_000_000, 2_372_000_000, 5_992_000_000]
    assert filing_items(FACTS, '51143', date(2021, 12, 31), ACCESSION, LATEST)['operating_income'].value == 5_992_000_000
    assert c.operating_income_label == 'EBIT proxy'
    assert 'financing interest' in c.valuation_note
    assert 'non-GAAP' in c.valuation_note
    latest = c.periods[0].income_statement
    assert latest.income_before_tax.value == 10_328_000_000
    assert latest.interest_expense.value == 1_935_000_000
    assert latest.income_tax_expense.value == -242_000_000  # Tax benefit, not a positive expense.
    assert latest.operating_income.source == 'derived' and latest.operating_income.confidence == .65
    assert ACCESSION in latest.operating_income.source_quote
    assert '0000051143-25-000015' in c.periods[3].income_statement.operating_income.source_quote
    assert any(f.field_path == 'income_statement.operating_income' for f in c.extraction_flags)
    a = default_assumptions(c);p = compute_projection(c, a)
    assert p.fair_value_per_share == pytest.approx(229.04073361049188) and len(p.years) == 5
    assert a.operating_margin == pytest.approx(sum(float(p.income_statement.operating_income.value / p.income_statement.revenue.value) for p in c.periods) / len(c.periods))


@pytest.mark.parametrize('mutation', ['entity', 'unit', 'start', 'end', 'quarter', 'form', 'accession', 'conflict', 'nonfinite', 'negative_interest', 'missing_interest', 'mixed_filing'])
def test_ibm_rejects_unsafe_operands_instead_of_guessing(mutation):
    facts = copy.deepcopy(FACTS)
    if mutation == 'entity': facts['cik'] = 320193
    data = facts['facts']['us-gaap'][CONCEPTS['interest_expense']]
    if mutation == 'unit':data['units']['EUR'] = data['units'].pop('USD')
    for row in data['units'].get('USD', []):
        if mutation == 'start':row['start'] = '2025-10-01'
        if mutation == 'end':row['end'] = '2025-12-30'
        if mutation == 'quarter':row['fp'] = 'Q4'
        if mutation == 'form':row['form'] = '10-Q'
        if mutation == 'accession':row['accn'] = '0000051143-26-999999'
        if mutation == 'mixed_filing':row['accn'] = '0000051143-25-000015'
        if mutation == 'nonfinite':row['val'] = 'NaN'
        if mutation == 'negative_interest':row['val'] = -1
    if mutation == 'conflict':
        row = next(r for r in data['units']['USD'] if r['end'] == '2025-12-31');data['units']['USD'].append({**row, 'val':1})
    if mutation == 'missing_interest':data['units']['USD'] = []
    assert filing_items(facts, '51143', LATEST, ACCESSION, LATEST) == {}


def test_ibm_equal_duplicates_are_safe_and_other_filers_are_untouched():
    facts = copy.deepcopy(FACTS)
    for obj in facts['facts']['us-gaap'].values():
        for rows in obj['units'].values():rows.extend(copy.deepcopy(rows))
    assert filing_items(facts, '51143', LATEST, ACCESSION, LATEST) == filing_items(FACTS, '51143', LATEST, ACCESSION, LATEST)
    assert filing_items(FACTS, '320193', LATEST, ACCESSION, LATEST) == {}


def test_ibm_native_operating_income_is_not_replaced():
    facts = copy.deepcopy(FACTS);row = copy.deepcopy(facts['facts']['us-gaap'][CONCEPTS['interest_expense']]['units']['USD'][-1])
    row.update(start='2025-01-01',end='2025-12-31',accn=ACCESSION,val=100)
    facts['facts']['us-gaap']['OperatingIncomeLoss'] = {'units':{'USD':[row]}}
    result = asyncio.run(track_a(state(facts)))
    assert result['periods_items'][LATEST]['operating_income'].value == 100


def test_ibm_fresh_pipeline_succeeds_when_ai_has_no_financial_statement_text(monkeypatch):
    import graph
    seen = []
    class Edgar:
        async def get_filing_html(self, url):return '<html>Statements incorporated by reference.</html>'
    async def backfill(**kwargs):seen.extend(kwargs['fields_to_extract']);return {}
    async def segments(**kwargs):return []
    monkeypatch.setattr(graph, 'extract_track_b', backfill);monkeypatch.setattr(graph, 'extract_revenue_segments', segments)
    s = state();s.update(asyncio.run(track_a(s)))
    c = asyncio.run(_make_track_b(Edgar(), None)(s))['company']
    assert c.periods[0].income_statement.operating_income.value == 12_263_000_000
    assert not set(CONCEPTS).intersection(seen) and 'operating_income' not in seen


def test_ibm_missing_tagged_operand_fails_even_if_ai_offers_an_operating_value(monkeypatch):
    import graph
    class Edgar:
        async def get_filing_html(self, url):return '<html>incorporated</html>'
    offered = company().periods[0].income_statement.operating_income
    async def backfill(**kwargs):return {'operating_income':offered}
    async def segments(**kwargs):return []
    monkeypatch.setattr(graph, 'extract_track_b', backfill);monkeypatch.setattr(graph, 'extract_revenue_segments', segments)
    facts = copy.deepcopy(FACTS);facts['facts']['us-gaap'].pop('InterestExpense')
    s = state(facts);s.update(asyncio.run(track_a(s)))
    with pytest.raises(CompositionError, match='operating_income'):asyncio.run(_make_track_b(Edgar(), None)(s))


def test_ibm_saved_review_survives_reads_and_valuation(monkeypatch):
    from app import main as api
    c = validate_company(apply_override(company(), 'income_statement.operating_income', Decimal(12_263_000_000), 'Reviewed proxy calculation test note; not a live human review'))
    repo = InMemoryRepo();asyncio.run(repo.set('IBM', c));monkeypatch.setattr(api, '_repo', repo)
    client = TestClient(api.app)
    assert Company.model_validate(client.get('/company/IBM').json()) == c
    result = client.post('/value/IBM', json={'assumptions':client.get('/value/IBM/defaults').json()})
    assert result.status_code == 200
    assert Company.model_validate(client.get('/company/IBM').json()) == c


def test_ibm_cashflow_addback_includes_amortization_and_debt_includes_current_borrowings():
    c = company()
    assert [p.cash_flow_statement.depreciation_amortization.value for p in c.periods] == [5_021_000_000, 4_667_000_000, 4_396_000_000, 4_802_000_000, 6_417_000_000]
    p = c.periods[0]
    assert p.balance_sheet.short_term_debt.value == 6_424_000_000
    assert p.balance_sheet.long_term_debt.value == 54_836_000_000
    assert p.cash_flow_statement.depreciation_amortization == p.income_statement.depreciation_amortization
    assert '2284000000 + 2737000000' in p.cash_flow_statement.depreciation_amortization.source_quote
    assert compute_projection(c, default_assumptions(c)).net_debt == 51_020_000_000


@pytest.mark.parametrize('mutation', ['amortization_missing', 'amortization_quarter', 'amortization_conflict', 'current_debt_unit', 'current_debt_duration', 'noncurrent_conflict'])
def test_ibm_rejects_incomplete_or_mismatched_cashflow_and_debt_inputs(mutation):
    facts = copy.deepcopy(FACTS)
    tag = 'AmortizationOfIntangibleAssets' if mutation.startswith('amortization') else 'ShortTermBorrowings' if mutation.startswith('current') else 'LongTermDebtNoncurrent'
    data = facts['facts']['us-gaap'][tag]
    if mutation == 'amortization_missing':data['units']['USD'] = []
    if mutation == 'current_debt_unit':data['units']['EUR'] = data['units'].pop('USD')
    for row in data['units'].get('USD', []):
        if mutation == 'amortization_quarter':row['start'] = '2025-10-01'
        if mutation == 'current_debt_duration':row['start'] = '2025-01-01'
    if mutation.endswith('conflict'):
        row = next(r for r in data['units']['USD'] if r['end'] == '2025-12-31');data['units']['USD'].append({**row, 'val':1})
    values = model_items(facts, '51143', LATEST, ACCESSION, LATEST)
    assert ('depreciation_amortization' not in values) if mutation.startswith('amortization') else ('short_term_debt' not in values and 'long_term_debt' not in values)


def test_ibm_missing_current_debt_cannot_silently_be_assumed_zero(monkeypatch):
    import graph
    class Edgar:
        async def get_filing_html(self, url):return '<html>incorporated</html>'
    async def backfill(**kwargs):return {}
    async def segments(**kwargs):return []
    monkeypatch.setattr(graph, 'extract_track_b', backfill);monkeypatch.setattr(graph, 'extract_revenue_segments', segments)
    facts = copy.deepcopy(FACTS);facts['facts']['us-gaap'].pop('ShortTermBorrowings')
    s = state(facts);s.update(asyncio.run(track_a(s)))
    with pytest.raises(CompositionError, match='debt coverage'):asyncio.run(_make_track_b(Edgar(), None)(s))
