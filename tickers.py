"""Use SEC/Yahoo's hyphen form for share-class ticker aliases."""


def normalize_ticker(ticker: str) -> str:
    return ticker.strip().upper().replace(".", "-")
