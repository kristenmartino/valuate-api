"""One layout-independent source-coverage statement for four public documents."""

import argparse
from pathlib import Path
import re
import urllib.request
import json

ROOT = Path(__file__).resolve().parents[1]
START = "<!-- valuate:source-coverage:start -->"
END = "<!-- valuate:source-coverage:end -->"
MDX_START = "{/* valuate:source-coverage:start */}"
MDX_END = "{/* valuate:source-coverage:end */}"
TARGETS = {"valuate-api": "README.md", "valuate-web": "README.md", "valuate": "README.md", "portfolio-v2": "src/content/work/valuate.mdx"}


def canonical():
    content = (ROOT / "docs/source-coverage.md").read_text().strip()
    cases = json.loads((ROOT / "tests/fixtures/reusable/index.json").read_text())
    qualified = [c["ticker"] for c in cases if c["expected_status"] == "qualified"]
    withheld = [c["ticker"] for c in cases if c["expected_status"] != "qualified"]
    names = lambda values: ", ".join(values[:-1]) + " and " + values[-1] if len(values) > 1 else values[0]
    outcomes = (names(qualified) + " qualify" if qualified else "No cases qualify")
    if withheld:
        outcomes += "; " + names(withheld) + (" is" if len(withheld) == 1 else " are") + " withheld for unresolved source/model coverage"
    content = content.replace("{{COLD_OUTCOMES}}", outcomes + ".")
    notes = " ".join(c["ticker"] + ": " + c["withheld_reason"].rstrip(".") + "." for c in cases if c["expected_status"] != "qualified" and c.get("withheld_reason"))
    content = content.replace("{{WITHHOLDING_NOTES}} ", notes + " " if notes else "")
    if re.search(r"\b(?:above|below)\b|\bsection\s+\d", content, re.I):
        raise ValueError("Shared source-coverage copy must not depend on page position or section numbers")
    return START + "\n" + content + "\n" + END


def sync(text, expected):
    start, end = START, END
    if MDX_START in text or MDX_END in text:
        if START in text or END in text:
            raise ValueError("Mixed Markdown/MDX source-coverage markers")
        start, end = MDX_START, MDX_END
        expected = expected.replace(START, start).replace(END, end)
    if text.count(start) != 1 or text.count(end) != 1:
        raise ValueError("Exactly one explicit source-coverage block is required")
    begin, finish = text.index(start), text.index(end) + len(end)
    if finish <= begin:
        raise ValueError("Source-coverage block markers are reversed")
    return text[:begin] + expected + text[finish:]


def check_remote(expected):
    for repo, path in TARGETS.items():
        url = f"https://raw.githubusercontent.com/kristenmartino/{repo}/main/{path}"
        with urllib.request.urlopen(url, timeout=60) as response:
            text = response.read().decode()
        if sync(text, expected) != text:
            raise ValueError(f"Published source-coverage statement drifted: {repo}/{path}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", type=Path, help="Folder containing the four repository checkouts")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--check-remote", action="store_true")
    args = parser.parse_args()
    expected = canonical()
    targets = [(ROOT / "README.md")] if args.workspace is None else [args.workspace / repo / path for repo, path in TARGETS.items()]
    for target in targets:
        old = target.read_text(); new = sync(old, expected)
        if args.check and old != new:
            raise ValueError(f"Source-coverage copy differs from the canonical statement: {target}")
        if not args.check:
            target.write_text(new)
    if args.check_remote:
        check_remote(expected)
    print("Source-coverage statements match the canonical, layout-independent copy")


if __name__ == "__main__":
    main()
