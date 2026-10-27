"""Statement roles, bounded history, preservation, and source-only onboarding."""
import asyncio
import copy
from datetime import date
from decimal import Decimal

import pytest
from bs4 import BeautifulSoup

from eval.cold import FixtureEdgar, FIXTURES, run_fixture
from eval.preflight import source_preflight
from graph import CompositionError, validate_company
from overrides import apply_override
from qualification_service import qualify_record
from statement_coverage import DEFAULT_CASH, policy, load_statement_coverage, repair_statement_history
from statement_evidence import read_statement_facts, select_statement_fact, statement_role
from tests.test_qualification import api_client


def load(f):
    p = f.saved.periods[0]
    return asyncio.run(load_statement_coverage(f, f.facts, f.saved.cik, p.fiscal_period_end, p.filing_accession,
        [date.fromisoformat(s) for s in policy(f.saved.cik,p.filing_accession)['period_ends']]))


def repair(f):
    return validate_company(repair_statement_history(f.saved, f.facts, load(f)))


def test_five_year_recovery_keeps_statement_roles_and_original_fields():
    f=FixtureEdgar('SBUX');c=repair(f)
    assert len(f.saved.periods)==2 and len(c.periods)==5
    assert [p.balance_sheet.cash_and_equivalents.value for p in c.periods] == [3219800000,3286200000,3551500000,2818400000,6455700000]
    assert [p.cash_flow_statement.depreciation_amortization.value for p in c.periods] == [1771500000,1592400000,1450300000,1529400000,1524100000]
    assert [p.income_statement.depreciation_amortization.value for p in c.periods] == [1684700000,1512600000,1362600000,1447900000,1441700000]
    p=c.periods[0]
    assert p.balance_sheet.long_term_debt.value==14575900000
    assert p.balance_sheet.short_term_debt.value==1498900000
    assert [p.balance_sheet.short_term_debt.value for p in c.periods]==[1498900000,1248900000,1852100000,1924000000,998900000]
    assert p.balance_sheet.long_term_debt.value+p.balance_sheet.short_term_debt.value==16074800000
    assert not c.extraction_flags
    paths=set(policy(c.cik,p.filing_accession)['statement_inputs'])|set(policy(c.cik,p.filing_accession)['required_history_context'])|{'balance_sheet.short_term_debt','balance_sheet.long_term_debt'}
    for old in f.saved.periods:
        new=next(p for p in c.periods if p.fiscal_period_end==old.fiscal_period_end)
        for stmt in ['income_statement','balance_sheet','cash_flow_statement']:
            for field in type(getattr(old,stmt)).model_fields:
                if stmt+'.'+field not in paths:
                    assert getattr(getattr(new,stmt),field)==getattr(getattr(old,stmt),field)
    checked,evidence=asyncio.run(qualify_record(c,f,f.facts))
    assert checked.qualification.status=='qualified'
    assert len(evidence['documents'])==2
    assert all(i.operands for i in checked.qualification.inputs if i.field_path in policy(c.cik,p.filing_accession)['statement_inputs'])
    cold,_=asyncio.run(run_fixture('SBUX'))
    assert cold.qualification.status=='qualified' and len(cold.periods)==5


@pytest.mark.parametrize('mutation',['entity','document_end','quarter','dimension','unit','nil','hidden','continued','conflict','label','role','duration','negative','scale'])
def test_broad_cash_requires_unambiguous_balance_sheet_presentation(mutation):
    html=(FIXTURES/'starbucks-2025-statements.xml').read_text()
    soup=BeautifulSoup(html,'xml')
    target=next(f for f in soup.find_all('nonFraction') if f.get('name')=='us-gaap:CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents' and f.find_parent('table') and statement_role(f.find_parent('table'))=='balance_sheet')
    context=soup.find('context',id=target['contextRef']);end=date(2025,9,28)
    if mutation in {'entity','document_end','quarter'}:
        key={'entity':'dei:EntityCentralIndexKey','document_end':'dei:DocumentPeriodEndDate','quarter':'dei:DocumentType'}[mutation]
        soup.find('nonNumeric',attrs={'name':key}).string={'entity':'999999','document_end':'2026-09-28','quarter':'10-Q'}[mutation]
    elif mutation=='dimension':
        context.append(soup.new_tag('xbrldi:explicitMember'))
    elif mutation=='unit':target['unitRef']='NOT_USD'
    elif mutation=='nil':target['xsi:nil']='true'
    elif mutation=='hidden':
        tag=soup.new_tag('ix:hidden');target.wrap(tag)
    elif mutation=='continued':target['continuedAt']='more'
    elif mutation=='conflict':
        dup=copy.copy(target);dup.string='1';target.insert_after(dup)
    elif mutation=='label':target.find_parent('tr').find('td').string='Cash, cash equivalents and restricted cash'
    elif mutation=='role':
        table=target.find_parent('table')
        for row in list(table.find_all('tr')):
            if target not in row.descendants:row.decompose()
    elif mutation=='duration':
        instant=context.find('instant');instant.name='endDate';start=soup.new_tag('xbrli:startDate');start.string='2025-07-01';instant.insert_before(start)
    elif mutation=='negative':target['sign']='-'
    elif mutation=='scale':target['scale']='not-a-number'
    rows=read_statement_facts(str(soup),'829224',end)
    assert select_statement_fact(rows,DEFAULT_CASH,end) is None


@pytest.mark.parametrize('broken',['cash','historical_da','tax','debt'])
def test_missing_history_cannot_be_silently_dropped_or_guessed(monkeypatch,broken):
    import eval.cold as cold
    f=FixtureEdgar('SBUX')
    tag={'cash':'CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents','historical_da':'DepreciationDepletionAndAmortization','tax':'IncomeTaxExpenseBenefit','debt':'LongTermDebt'}[broken]
    f.facts['facts']['us-gaap'][tag]['units']['USD']=[r for r in f.facts['facts']['us-gaap'][tag]['units']['USD'] if r['end']!='2022-10-02']
    original=f.saved.model_dump_json()
    with pytest.raises(ValueError):repair(f)
    monkeypatch.setattr(cold,'FixtureEdgar',lambda ticker:f)
    with pytest.raises(CompositionError,match='shorten the model window'):asyncio.run(run_fixture('SBUX'))
    assert f.saved.model_dump_json()==original


def test_same_filing_fact_must_match_inline_amount_and_current_debt_total():
    f=FixtureEdgar('SBUX')
    for row in f.facts['facts']['us-gaap']['CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents']['units']['USD']:
        if row['end']=='2025-09-28':row['val']+=1
    assert load(f)['missing']
    with pytest.raises(ValueError):repair(f)
    f=FixtureEdgar('SBUX')
    for row in f.facts['facts']['us-gaap']['LongTermDebt']['units']['USD']:
        if row['end']=='2025-09-28':row['val']+=1
    with pytest.raises(ValueError):repair(f)


def test_review_values_notes_and_coupled_debt_are_never_overwritten():
    f=FixtureEdgar('SBUX')
    f.saved=apply_override(f.saved,'balance_sheet.long_term_debt',Decimal(1),'Keep my full review note')
    f.saved=apply_override(f.saved,'cash_flow_statement.depreciation_amortization',Decimal(2),'Unresolved add-back review')
    before=f.saved.model_dump_json();c=repair(f)
    assert c.periods[0].balance_sheet.long_term_debt==f.saved.periods[0].balance_sheet.long_term_debt
    assert c.periods[0].balance_sheet.short_term_debt==f.saved.periods[0].balance_sheet.short_term_debt
    assert c.periods[0].cash_flow_statement.depreciation_amortization==f.saved.periods[0].cash_flow_statement.depreciation_amortization
    checked,_=asyncio.run(qualify_record(c,f,f.facts))
    assert checked.qualification.status=='review_required'
    assert f.saved.model_dump_json()==before
    assert repair_statement_history(c,f.facts,load(f)).valuation_note==c.valuation_note


@pytest.mark.parametrize('broken',[False,True])
def test_admin_source_repair_is_atomic_and_public_gate_is_enforced(monkeypatch,broken):
    from app import main as api
    f=FixtureEdgar('SBUX');original=f.saved
    client,repo=api_client(monkeypatch,original)
    assert client.post('/admin/repair-coverage/SBUX').status_code==401
    if broken:f.facts['facts']['us-gaap']['DepreciationDepletionAndAmortization']['units']['USD']=[]
    monkeypatch.setattr(api,'EdgarClient',lambda:f)
    async def checked(company,facts):return await qualify_record(company,f,facts)
    monkeypatch.setattr(api,'qualify_record',checked)
    r=client.post('/admin/repair-coverage/SBUX',headers={'Authorization':'Bearer qualification-unit-test-token'},json={})
    assert r.status_code==(422 if broken else 200),r.text
    active=asyncio.run(repo.get('SBUX'))
    assert (active==original) if broken else (len(active.periods)==5 and active.qualification.status=='qualified')
    assert client.get('/value/SBUX/defaults').status_code==(422 if broken else 200)


def test_source_preflight_cannot_grant_qualification_or_call_ai():
    f=FixtureEdgar('SBUX');before=f.saved.model_dump_json()
    result=asyncio.run(source_preflight('SBUX',f))
    assert result['source_checklist_complete'] and result['reviewed_filing_policy']
    assert len(result['periods'])==5 and result['qualification_granted'] is False
    assert f.saved.model_dump_json()==before


def test_statement_cash_recovery_generalizes_without_approving_another_filer():
    f=FixtureEdgar('SBUX');old_get=f.get_filing_html
    f.facts['cik']=12345
    async def html(url):return (await old_get(url)).replace('0000829224','0000012345')
    f.get_filing_html=html
    ends=[date(2025,9,28),date(2024,9,29),date(2023,10,1)]
    loaded=asyncio.run(load_statement_coverage(f,f.facts,'12345',ends[0],f.saved.periods[0].filing_accession,ends,candidate_cash_only=True))
    assert not loaded['missing'] and len(loaded['inputs'])==3
    assert not policy('12345',f.saved.periods[0].filing_accession)
    # A cash-flow row with that same broad concept is insufficient.
    async def cashflow_only(url):
        soup=BeautifulSoup(await html(url),'xml')
        for table in list(soup.find_all('table')):
            if statement_role(table)=='balance_sheet':table.decompose()
        return str(soup)
    f.get_filing_html=cashflow_only
    loaded=asyncio.run(load_statement_coverage(f,f.facts,'12345',ends[0],f.saved.periods[0].filing_accession,ends,candidate_cash_only=True))
    assert loaded['missing']

@pytest.mark.parametrize('broken',['historical_borrowing','zero_disclosure','wrong_zero_date'])
def test_separate_borrowings_and_latest_zero_require_evidence(broken):
    f=FixtureEdgar('SBUX')
    if broken=='historical_borrowing':
        rows=f.facts['facts']['us-gaap']['ShortTermBorrowings']['units']['USD']
        f.facts['facts']['us-gaap']['ShortTermBorrowings']['units']['USD']=[r for r in rows if r['end']!='2023-10-01']
    else:
        original=f.get_filing_html
        async def html(url):
            s=await original(url)
            quote=policy(f.saved.cik,f.saved.periods[0].filing_accession)['reviewed_input_scope']['additional_current_borrowings']['zero_disclosures']['2025-09-28'][0]
            return s.replace(quote, '' if broken=='zero_disclosure' else quote.replace('2025','2026'))
        f.get_filing_html=html
    with pytest.raises(ValueError):repair(f)


def test_latest_debt_without_an_independent_zero_proof_is_withheld():
    from qualification import qualify
    f=FixtureEdgar('SBUX');c=repair(f);loaded=load(f)
    q=qualify(c,f.facts,{'statement_inputs':loaded['inputs']})
    assert q.status=='review_required'
    assert any(i.field_path.endswith('debt') for i in q.issues)


def test_a_positive_borrowing_fact_cannot_be_overridden_by_a_zero_disclosure():
    f=FixtureEdgar('SBUX');p=f.saved.periods[0]
    f.facts['facts']['us-gaap']['ShortTermBorrowings']['units']['USD'].append({'end':str(p.fiscal_period_end),'val':1,'accn':p.filing_accession,'filed':'2025-11-14','form':'10-K','fp':'FY','fy':2025})
    with pytest.raises(ValueError):repair(f)
