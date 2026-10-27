"""Ownership-scope recovery and unsafe source refusals without issuer rules."""
import asyncio
import copy
from datetime import date
from decimal import Decimal
from unittest.mock import patch
import pytest
from filing_document import FilingDocument
from reusable_inputs import parent_equity_column
from schemas import Industry
from eval.reusable_cold import run_fixture

PARENT = 'us-gaap:StockholdersEquity'
TOTAL = 'us-gaap:StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest'
AXIS = 'us-gaap:StatementEquityComponentsAxis'
PE = date(2024, 12, 31)
FILING = {'accession': '0000000123-25-000001', 'url': 'https://www.sec.gov/Archives/edgar/data/123/000000012325000001/example.htm', 'filed': date(2025, 2, 20)}


def synthetic():
    """A different, synthetic filer with an older parent-column balance."""
    html = '''<html xmlns:ix="http://www.xbrl.org/2013/inlineXBRL" xmlns:xbrli="http://www.xbrl.org/2003/instance" xmlns:xbrldi="http://xbrl.org/2006/xbrldi"><body>
    <ix:nonNumeric name="dei:DocumentType">10-K</ix:nonNumeric><ix:nonNumeric name="dei:DocumentPeriodEndDate">2025-12-31</ix:nonNumeric><ix:nonNumeric name="dei:EntityCentralIndexKey">123</ix:nonNumeric>
    <xbrli:unit id="usd"><xbrli:measure>iso4217:USD</xbrli:measure></xbrli:unit>'''
    for key, member in [('total', None), ('parent', 'us-gaap:ParentMember'), ('nci', 'us-gaap:NoncontrollingInterestMember')]:
        dims = f'<xbrli:segment><xbrldi:explicitMember dimension="{AXIS}">{member}</xbrldi:explicitMember></xbrli:segment>' if member else ''
        html += f'<xbrli:context id="{key}"><xbrli:entity><xbrli:identifier scheme="http://www.sec.gov/CIK">123</xbrli:identifier>{dims}</xbrli:entity><xbrli:period><xbrli:instant>{PE}</xbrli:instant></xbrli:period></xbrli:context>'
    for key, amount in [('total', 103), ('parent', 100), ('nci', 3)]:
        html += f'<tr><td>Closing equity</td><td><ix:nonFraction name="{TOTAL}" contextRef="{key}" unitRef="usd" decimals="INF">{amount}</ix:nonFraction></td></tr>'
    html += '</body></html>'
    schema = '<schema>' + ''.join(f'<roleType roleURI="{role}"><definition>1 - Statement - {name}</definition></roleType>' for role, name in [('balance', 'Balance Sheet'), ('equity', 'Statement of Equity')]) + '</schema>'
    def link(kind, role, edges):
        tags = sorted({t for pair in edges for t in pair})
        locs = ''.join(f'<loc xlink:label="x{i}" xlink:href="x.xsd#{t.replace(":", "_")}"/>' for i, t in enumerate(tags))
        arcs = ''.join(f'<{kind}Arc xlink:from="x{tags.index(p)}" xlink:to="x{tags.index(c)}" weight="1"/>' for p, c in edges)
        return f'<{kind}Link xlink:role="{role}">{locs}{arcs}</{kind}Link>'
    balance = [('us-gaap:StatementTable', 'us-gaap:Assets'), ('us-gaap:StatementTable', 'us-gaap:LiabilitiesAndStockholdersEquity'), ('us-gaap:StatementTable', TOTAL), (TOTAL, PARENT), (TOTAL, 'us-gaap:MinorityInterest')]
    equity = [('us-gaap:StatementTable', TOTAL), ('us-gaap:StatementTable', AXIS), (AXIS, 'us-gaap:EquityComponentDomain'), ('us-gaap:EquityComponentDomain', 'us-gaap:ParentMember'), ('us-gaap:EquityComponentDomain', 'us-gaap:NoncontrollingInterestMember')]
    wrap = lambda s: '<linkbase xmlns:xlink="http://www.w3.org/1999/xlink">' + s + '</linkbase>'
    linked = {'schema': schema, 'presentation': wrap(link('presentation', 'balance', balance) + link('presentation', 'equity', equity)), 'calculation': wrap(link('calculation', 'balance', [(TOTAL, PARENT), (TOTAL, 'us-gaap:MinorityInterest')]))}
    facts = {'facts': {'us-gaap': {TOTAL.split(':')[1]: {'units': {'USD': [{'val': 103, 'end': str(PE), 'form': '10-K', 'fp': 'FY', 'accn': FILING['accession'], 'filed': str(FILING['filed'])}]}}}}}
    return html, linked, facts


def resolve(html, linked, facts):
    doc = FilingDocument(html, '0000000123', date(2025, 12, 31), linked)
    return parent_equity_column(doc, facts, FILING, FILING['filed'], PE)


def test_synthetic_filer_recovers_explicit_parent_column_with_complete_provenance():
    s = resolve(*synthetic())
    assert s.item.value == 100 and s.item.source.value == 'derived'
    assert s.item.xbrl_tag == PARENT
    assert len(s.rows) == 3 and all(r['concept'] == TOTAL for r in s.rows)
    assert s.rows[0]['dimensions'] == {AXIS: 'us-gaap:ParentMember'}
    assert 'NoncontrollingInterestMember' in s.item.source_quote
    assert 'ownership-scope reconciliation' in s.item.source_quote


@pytest.mark.parametrize('change', ['missing_parent', 'missing_nci', 'wrong_member', 'extra_dimension', 'wrong_date', 'duration', 'wrong_unit', 'hidden', 'conflict', 'sum', 'api', 'not_statement', 'missing_axis', 'missing_calculation'])
def test_incomplete_or_ambiguous_equity_never_infers_a_parent_balance(change):
    from bs4 import BeautifulSoup
    html, linked, facts = synthetic(); soup = BeautifulSoup(html, 'xml')
    parent = soup.find('nonFraction', attrs={'contextRef': 'parent'})
    ctx = soup.find('context', attrs={'id': 'parent'})
    if change == 'missing_parent': parent.decompose()
    if change == 'missing_nci': soup.find('nonFraction', attrs={'contextRef': 'nci'}).decompose()
    if change == 'wrong_member': ctx.find('explicitMember').string = 'srt:ParentCompanyMember'
    if change == 'extra_dimension':
        member = soup.new_tag('xbrldi:explicitMember', attrs={'dimension': 'srt:ScenarioAxis'});member.string = 'srt:ScenarioPreviouslyReportedMember';ctx.find('segment').append(member)
    if change == 'wrong_date': ctx.find('instant').string = '2024-12-30'
    if change == 'duration':
        ctx.find('instant').name = 'endDate';start = soup.new_tag('xbrli:startDate');start.string = '2024-01-01';ctx.find('period').append(start)
    if change == 'wrong_unit': parent['unitRef'] = 'eur'
    if change == 'hidden': parent.wrap(soup.new_tag('ix:hidden'))
    if change == 'conflict':
        duplicate = copy.copy(parent);duplicate.string = '101';parent.insert_after(duplicate)
    if change == 'sum': parent.string = '99'
    if change == 'api': facts['facts']['us-gaap'][TOTAL.split(':')[1]]['units']['USD'][0]['val'] = 104
    if change == 'not_statement': linked['schema'] = linked['schema'].replace('Statement - Statement of Equity', 'Disclosure - Statement of Equity')
    if change == 'missing_axis': linked['presentation'] = linked['presentation'].replace('us-gaap_StatementEquityComponentsAxis', 'test_OtherAxis')
    if change == 'missing_calculation': linked['calculation'] = '<linkbase/>'
    assert resolve(str(soup), linked, facts) is None


def test_conflicting_direct_parent_cannot_be_overridden_by_an_equity_column():
    html, linked, facts = synthetic()
    html = html.replace('</body>', f'<ix:nonFraction name="{PARENT}" contextRef="total" unitRef="usd" decimals="INF">99</ix:nonFraction></body>')
    assert resolve(html, linked, facts) is None


def test_reconciled_parent_column_and_direct_parent_total_share_one_history_scope():
    from history_consistency import validate_history
    from tests.test_history_consistency import field
    parent = resolve(*synthetic())
    earlier = date(2023, 12, 31)
    path = 'balance_sheet.shareholders_equity'
    sources = [(FILING, {PE: {path: parent}}),
               ({'accession': '0000000123-24-000001'}, {PE: {path: field(98, 'StockholdersEquity', start=None)}, earlier: {path: field(80, 'StockholdersEquity', start=None)}})]
    result = validate_history(sources, Industry.INSURER)
    assert not result['issues'] and len(result['revisions']) == 1
    assert result['revisions'][0]['new_value'] == '100'


def test_met_restatement_uses_parent_comparatives_not_original_or_consolidated_equity():
    company, _ = asyncio.run(run_fixture('MET'))
    assert company.qualification.status == 'qualified', company.qualification.issues
    assert [p.balance_sheet.shareholders_equity.value for p in company.periods] == list(map(Decimal, ['28398000000', '27445000000', '30015000000', '29881000000', '49746000000']))
    assert company.periods[-1].income_statement.net_income.value == Decimal('6855000000')
    evidence = next(i for i in company.qualification.inputs if i.field_path.endswith('shareholders_equity') and i.period_end == '2021-12-31')
    assert evidence.accession == '0001099219-24-000035'
    assert any('ParentMember' in r.get('dimensions', '') for r in evidence.operands)
    assert evidence.value != '50013000000' and evidence.value != '67482000000'


def test_equal_amount_with_wrong_saved_equity_scope_cannot_qualify():
    from reusable_qualification import qualify_reusable
    from reusable_inputs import load_reusable_inputs
    from eval.reusable_cold import ReusableEdgar
    c, _ = asyncio.run(run_fixture('MET'));c = c.model_copy(deep=True)
    client = ReusableEdgar('MET');p = c.periods[0]
    loaded = asyncio.run(load_reusable_inputs(client, client.facts, c.cik, p.fiscal_period_end, p.filing_accession, Industry.INSURER))
    c.periods[-1].balance_sheet.shareholders_equity.xbrl_tag = TOTAL
    q = qualify_reusable(c, client.facts, loaded, p.filing_accession)
    assert q.status == 'review_required' and any(i.code == 'equity_scope_unverified' for i in q.issues)
