"""Keep the published backend test inventory tied to actual pytest collection."""

from collections import Counter
from pathlib import Path
import argparse
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def collect(marker):
    run = subprocess.run([sys.executable, "-m", "pytest", "tests/", "--collect-only", "-q", "-m", marker], cwd=ROOT, capture_output=True, text=True, check=True)
    return Counter(re.findall(r"^tests/(test_[^/:]+\.py)::", run.stdout, re.M))


def update(text, selected, network):
    counts = selected + network
    summary = f"{sum(counts.values())} collected tests across {len(counts)} files: {sum(selected.values())} are selected by default, and the network integration test is deselected."
    text, n = re.subn(r"\d+ collected tests across \d+ files: \d+ are selected by default, and the network integration test is deselected\.", summary, text)
    if n != 1:
        raise ValueError("Exactly one explicit test-inventory summary is required")
    documented = set()
    def replace(match):
        name = match[1]
        if name not in counts: raise ValueError(f"Documented test file is absent: {name}")
        documented.add(name)
        return f"- `{name}` ({counts[name]}{match[3] or ''})"
    text = re.sub(r"^- `(test_[^`]+\.py)` \((\d+)(, network-gated)?\)", replace, text, flags=re.M)
    if documented != set(counts):
        raise ValueError(f"Test files need a documented purpose: {sorted(set(counts)-documented)}")
    return text


def main():
    parser = argparse.ArgumentParser(); parser.add_argument("--write", action="store_true"); args = parser.parse_args()
    p = ROOT / "README.md"; old = p.read_text()
    expected = update(old, collect("not network"), collect("network"))
    if args.write: p.write_text(expected)
    elif old != expected: raise ValueError("Published test inventory drifted; run python -m ops.check_test_inventory --write")
    print("Published backend test counts match pytest collection")


if __name__ == "__main__": main()
