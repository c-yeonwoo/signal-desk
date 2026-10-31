"""Export one captured decision into the installed financial-skill workflow."""
from __future__ import annotations

import argparse
import datetime as dt
import gzip
import hashlib
import json
from pathlib import Path

from signal_desk.signals import decision_price_sensitivity, research_packet


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--as-of", required=True, help="Explicit ISO timestamp with timezone")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("output exists; preserve prior research evidence")
    with gzip.open(args.bundle, "rb") as stream:
        raw = stream.read(decision_price_sensitivity.MAX_BYTES + 1)
    if len(raw) > decision_price_sensitivity.MAX_BYTES:
        parser.error("capture exceeds size limit")
    result = research_packet.build(json.loads(raw),
        as_of=dt.datetime.fromisoformat(args.as_of.replace("Z", "+00:00")))
    result["bundle_file_sha256"] = hashlib.sha256(args.bundle.read_bytes()).hexdigest()
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, allow_nan=False, indent=2)
    print(json.dumps({"output": str(args.output), "market": result["market"],
                      "items": result["universe_size"], "purpose": result["purpose"]}))


if __name__ == "__main__":
    main()
