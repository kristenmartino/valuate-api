"""Share-class aliases must share SEC lookup, cache, value and correction state."""
import asyncio
import pytest
from fastapi.testclient import TestClient
from edgar import EdgarClient
from repository import InMemoryRepo, PostgresRepo
from tickers import normalize_ticker
from tests.test_overrides import _company, _std_period


@pytest.mark.parametrize('ticker', ['BRK.B', 'BRK-B', ' brk.b ', 'brk-b'])
def test_sec_alias_lookup(ticker):
    async def lookup():
        client = EdgarClient(user_agent='Valuate test qa@example.com')
        client._ticker_cik_map = {'BRK-B': '0001067983', 'AAPL': '0000320193'}
        return await client.get_cik_from_ticker(ticker)
    assert asyncio.run(lookup()) == '0001067983'
    assert normalize_ticker(ticker) == 'BRK-B'


def test_unknown_ticker_is_not_mapped_to_a_known_company():
    async def lookup():
        client = EdgarClient(user_agent='Valuate test qa@example.com')
        client._ticker_cik_map = {'BRK-B': '0001067983'}
        return await client.get_cik_from_ticker('ZZZZ')
    with pytest.raises(ValueError, match='not found'):
        asyncio.run(lookup())


def test_api_aliases_share_extract_read_value_and_correction(monkeypatch):
    from app import main as api
    from app.rate_limit import require_extract_rate_limit
    from schemas import CompsResponse
    company = _company(_std_period(), ticker='BRK-B')
    calls = []
    class Graph:
        async def ainvoke(self, state):
            calls.append(state['ticker'])
            return {'company': company}
    async def comps(ticker):
        calls.append(ticker)
        return CompsResponse(target_ticker=ticker, peers=[])
    monkeypatch.setattr(api, '_graph', Graph())
    monkeypatch.setattr(api, '_repo', InMemoryRepo())
    monkeypatch.setattr(api, 'get_peer_comps', comps)
    monkeypatch.delenv('VALUATE_OVERRIDE_TOKEN', raising=False)
    api.app.dependency_overrides[require_extract_rate_limit] = lambda: None
    try:
        client = TestClient(api.app)
        assert client.post('/extract', json={'ticker':' brk.b '}).json()['ticker'] == 'BRK-B'
        assert calls == ['BRK-B']
        dot = client.get('/company/BRK.B'); dash = client.get('/company/BRK-B')
        assert dot.status_code == dash.status_code == 200
        assert dot.json() == dash.json()
        a = client.get('/value/BRK.B/defaults').json()
        assert a == client.get('/value/BRK-B/defaults').json()
        request = {'assumptions':a}
        assert client.post('/value/BRK.B',json=request).json() == client.post('/value/BRK-B',json=request).json()
        updated = client.put('/company/BRK.B/override',json={'field_path':'income_statement.operating_income','value':'250'})
        assert updated.status_code == 200
        assert client.get('/company/BRK-B').json()['periods'][0]['income_statement']['operating_income']['value'] == '250'
        assert client.get('/comps/BRK.B').json()['target_ticker'] == 'BRK-B'
        assert calls[-1] == 'BRK-B'
    finally:
        api.app.dependency_overrides.pop(require_extract_rate_limit, None)


def test_postgres_uses_the_same_key_for_dot_and_hyphen_aliases():
    keys = []
    company = _company(_std_period(), ticker='BRK-B')
    class Connection:
        async def fetchrow(self, sql, ticker):
            keys.append(ticker)
            return {'payload': company.model_dump(mode='json')}
        def transaction(self): return Acquire()
        async def execute(self, sql, *args):
            if sql.startswith("INSERT INTO companies"): keys.append(args[0])
    class Acquire:
        async def __aenter__(self): return Connection()
        async def __aexit__(self, *args): pass
    class Pool:
        def acquire(self): return Acquire()
    repo = PostgresRepo('unused')
    repo._pool = Pool()
    asyncio.run(repo.get(' brk.b '))
    asyncio.run(repo.set('BRK-B', company))
    assert keys == ['BRK-B', 'BRK-B', 'BRK-B']
