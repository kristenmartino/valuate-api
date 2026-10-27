"""Authenticated one-time import and qualification; never print credentials."""

import argparse
import concurrent.futures
import json
import os
from pathlib import Path
import time
import urllib.error
import urllib.request


def request(base, path, body=None):
    token = os.environ.get("VALUATE_OVERRIDE_TOKEN")
    if not token:
        raise RuntimeError(
            "Run this client with the existing backend service environment"
        )
    req = urllib.request.Request(
        base.rstrip("/") + "/" + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + token,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=240) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as exc:
        try:
            body = json.load(exc)
        except ValueError:
            body = {"detail": "Non-JSON error response"}
        return exc.code, body


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshots", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--base", default="https://valuate.kristenmartino.ai/api")
    parser.add_argument("--commit", required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    for _ in range(80):
        status, version = request(args.base, "version")
        if status == 200 and version.get("commit_sha") == args.commit:
            break
        time.sleep(3)
    else:
        raise RuntimeError("Expected backend release is not serving; nothing imported")
    status, health = request(args.base, "healthz")
    assert status == 200 and health.get("storage") == "postgres", (status, health)

    def restore(path):
        original = json.loads(path.read_text())
        ticker = original["ticker"]
        status, result = request(args.base, "admin/import/" + ticker, original)
        assert status in {200, 409}, (ticker, status, result)
        status, active = request(args.base, "company/" + ticker)
        assert status == 200, (ticker, status)
        assert {k: v for k, v in active.items() if k != "qualification"} == {
            k: v for k, v in original.items() if k != "qualification"
        }, (
            ticker,
            "Original snapshot differs; import did not overwrite the active record",
        )
        print(ticker, "imported; snapshot and reviews exact", flush=True)

    def qualify_import(path):
        original = json.loads(path.read_text())
        ticker = original["ticker"]
        status, result = request(args.base, "admin/qualify/" + ticker, {})
        assert status == 200, (ticker, status, result)
        status, active = request(args.base, "company/" + ticker)
        assert status == 200
        assert {k: v for k, v in active.items() if k != "qualification"} == {
            k: v for k, v in original.items() if k != "qualification"
        }, (ticker, "qualification changed financials or reviews")
        (args.output / ("company-" + ticker + ".json")).write_text(
            json.dumps(active, indent=2) + "\n"
        )
        print(
            ticker,
            "snapshot and reviews exact;",
            active["qualification"]["status"],
            flush=True,
        )

    # Reviewer records first; subsequent work is read-only qualification.
    files = sorted(
        args.snapshots.glob("company-*.json"),
        key=lambda p: (
            p.stem.removeprefix("company-") not in ["INTU", "CSCO", "CRM", "ADBE"],
            p.name,
        ),
    )
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(restore, files))
    # All snapshots are durable before any slower source qualification begins.
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(qualify_import, files))
    (args.output / "version.json").write_text(json.dumps(version, indent=2) + "\n")
    print("Persistent migration and qualification verified:", len(files), flush=True)


if __name__ == "__main__":
    main()
