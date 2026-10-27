"""Reviewed Oracle/Amazon source-only onboarding and fail-closed publication."""
import asyncio
import copy
from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import patch

from bs4 import BeautifulSoup
import pytest

from dcf import default_assumptions, compute_projection
from eval.cold import FixtureEdgar, run_fixture
from graph import build_graph, CompositionError
from overrides import apply_override
from qualification import qualify
from qualification_service import qualify_record
from scoped_inputs import contract, load_scoped_inputs
from tests.test_qualification import api_client


def load(f):
    c=f.saved;p=c.periods[0]
    return asyncio.run(load_scoped_inputs(f,f.facts,c.cik,p.fiscal_period_end,p.filing_accession,[x.fiscal_period_end for x in c.periods]))


@pytest.mark.parametrize('ticker,da,current,noncurrent,net_debt',[
    ('ORCL',[9294000000,6174000000,6139000000,6108000000,3122000000],7819000000,129423000000,136143000000),
    ('AMZN',[65756000000,52795000000,48663000000,41921000000,34433000000],5105000000,84190000000,91737000000),
])
def test_source_only_cold_graph_qualifies_full_model_history_without_ai(ticker,da,current,noncurrent,net_debt):
    async def no_ai(**kwargs):raise AssertionError('No AI is permitted in a reviewed source-only extraction')
    with patch('graph.extract_track_b',no_ai),patch('graph.extract_revenue_segments',no_ai):c,e=asyncio.run(run_fixture(ticker))
    assert c.qualification.status=='qualified',c.qualification.issues
    assert len(c.periods)==5 and len(e['documents'])==2
    assert [p.cash_flow_statement.depreciation_amortization.value for p in c.periods]==da
    assert all(p.income_statement.depreciation_amortization is None for p in c.periods)
    p=c.periods[0];assert p.balance_sheet.short_term_debt.value==current and p.balance_sheet.long_term_debt.value==noncurrent
    assert p.income_statement.interest_expense is not None
    v=compute_projection(c,default_assumptions(c));assert v.net_debt==net_debt
    # Qualification is source coverage, not a promise of positive modeled equity.
    assert (v.fair_value_per_share<0) if ticker=='ORCL' else (v.fair_value_per_share>0)
    assert not c.extraction_flags
    assert any(i.operands for i in c.qualification.inputs if i.field_path.endswith('debt'))
    if ticker=='ORCL':
        assert p.income_statement.income_before_tax.value==19554000000
        assert c.periods[2].balance_sheet.long_term_debt is None
    else:
        assert 'not pure D&A' in c.valuation_note
        evidence=next(i for i in c.qualification.inputs if i.field_path=='balance_sheet.long_term_debt')
        assert any(x['concept']=='amzn:FinancingObligationsNoncurrent' and x['value']=='7800000000' and x['decimals']=='-8' for x in evidence.operands)


@pytest.mark.parametrize('ticker',['ORCL','AMZN'])
@pytest.mark.parametrize('mutation',['entity','document_end','quarter','unit','hidden','continued','negative','conflict','dimension','label'])
def test_unsafe_statement_evidence_cannot_qualify_or_be_replaced(ticker,mutation):
    f=FixtureEdgar(ticker);original=f.get_filing_html;latest=f.saved.periods[0].fiscal_period_end
    da_tag=contract(f.saved.cik,f.saved.periods[0].filing_accession)['facts']['depreciation' if ticker=='ORCL' else 'addback']['concept']
    async def html(url):
        soup=BeautifulSoup(await original(url),'xml')
        if str(latest.year) not in url:return str(soup)
        target=next(x for x in soup.find_all('nonFraction') if x.get('name')==da_tag and (ctx:=soup.find('context',id=x['contextRef'])).find('endDate') is not None and ctx.find('endDate').get_text()==str(latest) and not ctx.find('explicitMember'))
        ctx=soup.find('context',id=target['contextRef'])
        if mutation=='entity':soup.find('nonNumeric',attrs={'name':'dei:EntityCentralIndexKey'}).string='999999'
        elif mutation=='document_end':soup.find('nonNumeric',attrs={'name':'dei:DocumentPeriodEndDate'}).string='2030-01-01'
        elif mutation=='quarter':ctx.find('startDate').string=str(latest-timedelta(days=90))
        elif mutation=='unit':target['unitRef']='EUR'
        elif mutation=='hidden':target.wrap(soup.new_tag('ix:hidden'))
        elif mutation=='continued':target['continuedAt']='untrusted-continuation'
        elif mutation=='negative':target['sign']='-'
        elif mutation=='dimension':
            m=soup.new_tag('xbrldi:explicitMember');m['dimension']='test:BusinessAxis';m.string='test:SubsidiaryMember';ctx.append(m)
        elif mutation=='label':target.find_parent('tr').find('td').string='A differently scoped adjustment'
        elif mutation=='conflict':
            other=copy.copy(target);other.string='1';target.insert_after(other)
        return str(soup)
    f.get_filing_html=html
    loaded=load(f);assert any(i['period_end']==str(latest) and i['field_path']=='cash_flow_statement.depreciation_amortization' for i in loaded['missing'])
    q=qualify(f.saved,f.facts,{'scoped_inputs':loaded['inputs']});assert q.status=='review_required'


@pytest.mark.parametrize('ticker,tag',[('ORCL','FinanceLeaseLiabilityCurrent'),('ORCL','DebtLongtermAndShorttermCombinedAmount'),('AMZN','FinanceLeaseLiabilityNoncurrent'),('AMZN','ShortTermBorrowings')])
@pytest.mark.parametrize('mutation',['missing','conflict','unit','future'])
def test_every_debt_operand_and_same_filing_reconciliation_are_required(ticker,tag,mutation):
    f=FixtureEdgar(ticker);p=f.saved.periods[0];data=f.facts['facts']['us-gaap'][tag]['units'];rows=data['USD'];targets=[r for r in rows if r['end']==str(p.fiscal_period_end) and r['accn']==p.filing_accession];assert targets
    if mutation=='missing':data['USD']=[r for r in rows if r not in targets]
    elif mutation=='unit':data['EUR']=data.pop('USD')
    elif mutation=='conflict':rows.append({**targets[0],'val':1})
    else:
        for r in targets:r['filed']='2030-01-01'
        # Conflicting filing dates invalidate the anchor itself before algebra.
        with pytest.raises(ValueError, match='Annual source filing date'):
            load(f)
        return
    loaded=load(f);assert any(x['field_path'].endswith('debt') and x['period_end']==str(p.fiscal_period_end) for x in loaded['missing'])
    assert qualify(f.saved,f.facts,{'scoped_inputs':loaded['inputs']}).status=='review_required'


@pytest.mark.parametrize('mutation',['discount_dimension','financing_context','changed_amount'])
def test_amazon_custom_financing_and_discount_contexts_are_not_inferred(mutation):
    f=FixtureEdgar('AMZN');original=f.get_filing_html
    async def html(url):
        soup=BeautifulSoup(await original(url),'xml')
        if '20251231' not in url:return str(soup)
        name='us-gaap:DebtInstrumentUnamortizedDiscountPremiumNet' if mutation=='discount_dimension' else 'amzn:FinancingObligationsNoncurrent'
        target=next(x for x in soup.find_all('nonFraction') if x.get('name')==name and soup.find('context',id=x['contextRef']).find('instant').get_text()=='2025-12-31')
        ctx=soup.find('context',id=target['contextRef'])
        if mutation=='discount_dimension':ctx.find('explicitMember').string='us-gaap:OtherDebtMember'
        elif mutation=='financing_context':ctx.find('instant').string='2026-12-31'
        else:target.string='8.8'
        return str(soup)
    f.get_filing_html=html;loaded=load(f)
    # A changed valid source amount must not validate the previously saved debt.
    assert qualify(f.saved,f.facts,{'scoped_inputs':loaded['inputs']}).status=='review_required'


@pytest.mark.parametrize('ticker',['ORCL','AMZN'])
def test_missing_model_history_stops_cold_composition_without_an_ai_fallback(ticker):
    f=FixtureEdgar(ticker);original=f.get_filing_html
    async def html(url):
        soup=BeautifulSoup(await original(url),'xml')
        for x in list(soup.find_all('nonFraction')):
            if x.get('name') in ['us-gaap:Depreciation','us-gaap:DepreciationDepletionAndAmortization']:
                ctx=soup.find('context',id=x['contextRef']);end=ctx.find('endDate')
                if end is not None and end.get_text().startswith('2022'):x.decompose()
        return str(soup)
    f.get_filing_html=html
    async def no_ai(**kwargs):raise AssertionError('AI must not repair a source contract')
    with patch('graph.extract_track_b',no_ai),patch('graph.extract_revenue_segments',no_ai),pytest.raises(CompositionError,match='refusing to shorten'):
        asyncio.run(build_graph(f,None).ainvoke({'ticker':ticker}))


@pytest.mark.parametrize('ticker',['ORCL','AMZN'])
def test_sec_only_preflight_is_a_checklist_and_never_grants_publication(ticker):
    from eval.preflight import source_preflight
    f=FixtureEdgar(ticker);result=asyncio.run(source_preflight(ticker,f))
    assert result['source_checklist_complete'] and result['reviewed_filing_policy']
    assert result['qualification_granted'] is False and len(result['periods'])==5


@pytest.mark.parametrize('ticker',['ORCL','AMZN'])
def test_staged_source_only_onboarding_preserves_same_filing_review(monkeypatch,ticker):
    from app import main as api
    f=FixtureEdgar(ticker);reviewed=apply_override(f.saved,'income_statement.selling_general_administrative',Decimal(1234),'Keep this complete independent non-model review')
    client,repo=api_client(monkeypatch,reviewed)
    monkeypatch.setattr(api,'_graph',build_graph(f,None))
    async def checked(company,facts=None):return await qualify_record(company,f,f.facts)
    monkeypatch.setattr(api,'qualify_record',checked)
    headers={'Authorization':'Bearer qualification-unit-test-token'}
    assert client.post('/admin/cold-extract',json={'ticker':ticker}).status_code==401
    response=client.post('/admin/cold-extract',headers=headers,json={'ticker':ticker});assert response.status_code==200,response.text
    assert asyncio.run(repo.get(ticker))==reviewed
    publish=client.post('/admin/publish/'+response.json()['candidate_id'],headers=headers,json={});assert publish.status_code==200,publish.text
    active=asyncio.run(repo.get(ticker));assert active.qualification.status=='qualified'
    assert active.periods[0].income_statement.selling_general_administrative==reviewed.periods[0].income_statement.selling_general_administrative
    assert client.get('/value/'+ticker+'/defaults').status_code==200


@pytest.mark.parametrize('ticker',['ORCL','AMZN'])
def test_conflicting_model_input_review_blocks_publication_without_overwrite(monkeypatch,ticker):
    from app import main as api
    f=FixtureEdgar(ticker)
    reviewed=apply_override(f.saved,'cash_flow_statement.depreciation_amortization',Decimal(42),'Retain this conflicting model-input review until resolved')
    client,repo=api_client(monkeypatch,reviewed)
    monkeypatch.setattr(api,'_graph',build_graph(f,None))
    async def checked(company,facts=None):return await qualify_record(company,f,f.facts)
    monkeypatch.setattr(api,'qualify_record',checked)
    headers={'Authorization':'Bearer qualification-unit-test-token'}
    response=client.post('/admin/cold-extract',headers=headers,json={'ticker':ticker})
    assert response.status_code==200,response.text
    publication=client.post('/admin/publish/'+response.json()['candidate_id'],headers=headers,json={})
    assert publication.status_code==422,publication.text
    assert asyncio.run(repo.get(ticker))==reviewed
    assert client.get('/value/'+ticker+'/defaults').status_code==422
