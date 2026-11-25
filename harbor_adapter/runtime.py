"""Run one Cartwheel evaluation case inside a Harbor sandbox."""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path
from typing import Any

from replay.rollout import fresh_world, retrieved_docs_text, run_case

_TRANSIENT_TOKENS = (
    "429",
    "500",
    "502",
    "503",
    "504",
    "ratelimit",
    "rate_limit",
    "resource_exhausted",
    "serviceunavailable",
    "service unavailable",
    "internalservererror",
    "badgateway",
    "apiconnectionerror",
    "timeout",
    "overloaded",
    "temporarily unavailable",
)


def _is_transient_infra_error(exc: Exception) -> bool:
    """Return True only for transport/rate-limit/server errors before a verdict."""
    haystack = f"{type(exc).__name__}: {exc}".lower()
    return any(token in haystack for token in _TRANSIENT_TOKENS)


def run(case_path: Path, output_path: Path, model: str) -> dict[str, Any]:
    """Run the case in a freshly seeded world and save verifier evidence.

    Retries ONLY on transient API/transport infrastructure failures (e.g. 429
    rate limits or 503 server errors), re-seeding a fresh world before each
    attempt. A completed transcript is never retried.
    """
    case = json.loads(case_path.read_text())
    state_root = Path("/app/data")
    backoff_seconds = (5, 15, 30, 60)
    transcript: dict[str, Any] | None = None
    for attempt in range(len(backoff_seconds) + 1):
        try:
            with fresh_world(state_root):
                transcript = run_case(case, model=model)
            break
        except Exception as exc:
            if attempt >= len(backoff_seconds) or not _is_transient_infra_error(exc):
                raise
            time.sleep(backoff_seconds[attempt])
    assert transcript is not None

    evidence = {
        "case_id": case["id"],
        "kind": case.get("kind", "unclassified"),
        "transcript": transcript,
        "retrieved_policy_documents": retrieved_docs_text(transcript),
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(evidence, indent=2, ensure_ascii=False) + "\n")
    return evidence


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", type=Path, default=Path("/app/case.json"))
    parser.add_argument(
        "--output", type=Path, default=Path("/app/cartwheel-result.json")
    )
    parser.add_argument("--model", default=os.environ.get("CARTWHEEL_MODEL"))
    args = parser.parse_args()
    if not args.model:
        parser.error("pass --model or set CARTWHEEL_MODEL")
    run(args.case, args.output, args.model)


if __name__ == "__main__":
    main()
