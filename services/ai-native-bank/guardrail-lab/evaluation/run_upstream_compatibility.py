"""Run the pinned upstream white/black corpus against the hardened input Rail."""

from __future__ import annotations

import argparse
import json
import os
import time
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path
from typing import Callable


VALID_DECISIONS = frozenset({"allow", "review", "block", "unavailable"})
DEFAULT_CORPUS = Path("/app/data/samples.json")
DEFAULT_TOKEN_FILE = Path("/run/ai-bank-secrets/runtime-token")


def load_corpus(path: Path) -> dict[str, list[dict[str, str]]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("upstream corpus must be an object")
    corpus: dict[str, list[dict[str, str]]] = {}
    for key in ("white_samples", "black_samples"):
        samples = payload.get(key)
        if not isinstance(samples, list) or not samples:
            raise ValueError(f"{key} must be a non-empty list")
        normalized: list[dict[str, str]] = []
        for item in samples:
            if not isinstance(item, dict):
                raise ValueError(f"{key} contains a non-object sample")
            label = item.get("label")
            text = item.get("text")
            if not isinstance(label, str) or not label.strip():
                raise ValueError(f"{key} sample label is invalid")
            if not isinstance(text, str) or not text.strip():
                raise ValueError(f"{key} sample text is invalid")
            normalized.append({"label": label, "text": text})
        corpus[key] = normalized
    return corpus


def classify_outcomes(
    corpus: dict[str, list[dict[str, str]]],
    evaluate: Callable[[str, str], str],
) -> dict[str, object]:
    counts: dict[str, Counter[str]] = {
        "white": Counter(),
        "black": Counter(),
    }
    white_blocks: list[int] = []
    black_allows: list[int] = []
    for group, key in (("white", "white_samples"), ("black", "black_samples")):
        for index, sample in enumerate(corpus[key], start=1):
            decision = evaluate(sample["text"], f"upstream-{group}-{index:03d}")
            if decision not in VALID_DECISIONS:
                decision = "unavailable"
            counts[group][decision] += 1
            if group == "white" and decision == "block":
                white_blocks.append(index)
            if group == "black" and decision == "allow":
                black_allows.append(index)
    return {
        "source": "upstream/data/samples.json",
        "white": {"total": len(corpus["white_samples"]), **dict(counts["white"])},
        "black": {"total": len(corpus["black_samples"]), **dict(counts["black"])},
        "whiteBlockIndexes": white_blocks,
        "blackAllowIndexes": black_allows,
        "passed": not white_blocks and not black_allows,
    }


def _token(token_file: Path) -> str:
    value = os.environ.get("OPENAI_API_KEY", "").strip()
    if not value and token_file.is_file():
        value = token_file.read_text(encoding="utf-8").strip()
    if not value:
        raise RuntimeError("guardrail service token is unavailable")
    return value


def make_http_evaluator(
    *,
    base_url: str,
    token: str,
    timeout_seconds: float,
) -> Callable[[str, str], str]:
    endpoint = base_url.rstrip("/") + "/v1/rails/input"

    def evaluate(text: str, request_id: str) -> str:
        body = json.dumps(
            {
                "message": text,
                "requestId": request_id,
                "conversationId": "upstream-compatibility",
                "history": [],
            },
            ensure_ascii=False,
        ).encode("utf-8")
        request = urllib.request.Request(
            endpoint,
            data=body,
            method="POST",
            headers={
                "authorization": "Bearer " + token,
                "content-type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError, urllib.error.HTTPError):
            return "unavailable"
        decision = payload.get("decision") if isinstance(payload, dict) else None
        return decision if isinstance(decision, str) else "unavailable"

    return evaluate


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--token-file", type=Path, default=DEFAULT_TOKEN_FILE)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--timeout-seconds", type=float, default=12.0)
    args = parser.parse_args()

    started = time.monotonic()
    corpus = load_corpus(args.corpus)
    evaluate = make_http_evaluator(
        base_url=args.base_url,
        token=_token(args.token_file),
        timeout_seconds=args.timeout_seconds,
    )
    result = classify_outcomes(corpus, evaluate)
    result["durationMs"] = int((time.monotonic() - started) * 1000)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
