"""End-to-end SSE intent-switch test via BFF (port 3000) → runtime (port 18120).

This script verifies Plan D Step 1 (ContextEngine cross-turn reuse) under the
real production path: BFF registers agent_run to its repository before
delegating to runtime, so node_gateway capability invocations resolve
agent_run_id without 404.

Scenario:
  R1: "我要转账100"  -> establish transfer supplement state
  R2: "有哪些理财推荐" -> should trigger CREATE_TASK switch

Verification points:
  1) SSE stream completes without timeout / abort
  2) No "本次服务未完成" wording
  3) decide() logs switched=True (CREATE_TASK) — check container logs
  4) ContextEngine pool_size>0 on R2 — check container logs
  5) node_gateway no longer returns 404 agent_run_not_found
"""
from __future__ import annotations

import json
import sys
import time

import httpx

BFF = "http://localhost:3000"
API = BFF + "/api/ai-bank/v1"
CUSTOMER_ID = "cust_001"
COMMON_HEADERS = {
    "x-mock-customer-id": CUSTOMER_ID,
    "x-actor-type": "agent",
    "accept": "text/event-stream",
}


def _create_session() -> str:
    """Create an agent session, return conversationId."""
    r = httpx.post(
        API + "/agent-sessions",
        headers={**COMMON_HEADERS, "accept": "application/json"},
        json={},
        timeout=30.0,
    )
    if r.status_code >= 400:
        print(f"[create-session FAIL] status={r.status_code} body={r.text[:400]}")
        raise SystemExit(1)
    body = r.json()
    data = body.get("data") if isinstance(body.get("data"), dict) else body
    session_id = (data.get("id") or body.get("id")
                  or body.get("sessionId") or body.get("conversationId"))
    if not session_id:
        print(f"[create-session FAIL] no session id in body: {body}")
        raise SystemExit(1)
    print(f"[create-session OK] conversationId={session_id}")
    return session_id


def _stream_sse(conversation_id: str, message: str, timeout: float = 120.0) -> dict:
    """Stream an agent-run via BFF, return summary.

    BFF SSE format:
      event: agent.trace\\n data: <json>\\n\\n
      event: agent.result\\n data: <json>\\n\\n
    """
    payload = {"conversationId": conversation_id, "message": message}
    started = time.time()
    traces: list[dict] = []
    final_status: str | None = None
    final_error: str | None = None
    trace_failed: str | None = None
    body_chunks: list[str] = []
    current_event_type: str | None = None

    with httpx.Client(timeout=httpx.Timeout(timeout, connect=10.0)) as client:
        with client.stream(
            "POST", API + "/agent-runs",
            headers=COMMON_HEADERS, json=payload,
        ) as resp:
            http_status = resp.status_code
            line_count = 0
            for line in resp.iter_lines():
                if not line:
                    current_event_type = None
                    continue
                body_chunks.append(line)
                line_count += 1
                if line_count <= 6:
                    print(f"  [raw line {line_count}] {line[:200]}")
                if line.startswith("event:"):
                    current_event_type = line[6:].strip()
                    continue
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if not data:
                    continue
                try:
                    evt = json.loads(data)
                except json.JSONDecodeError:
                    continue

                if current_event_type == "agent.trace" or evt.get("stage") or evt.get("stageName"):
                    traces.append(evt)
                    stage = evt.get("stage") or evt.get("stageName")
                    status = evt.get("status")
                    title = evt.get("title") or evt.get("message")
                    if stage and status:
                        print(f"  [trace] stage={stage} status={status} title={title}")
                    if stage == "run.failed" or status == "failed":
                        trace_failed = (evt.get("detail") or evt.get("message")
                                        or evt.get("title") or "failed")
                elif current_event_type == "agent.result" or "delivery" in evt:
                    final_status = evt.get("status") or final_status
                    if evt.get("error"):
                        final_error = evt.get("error")
                    print(f"  [result] status={final_status} error={final_error}")

    elapsed = time.time() - started
    raw = "\n".join(body_chunks)
    has_incomplete_wording = "本次服务未完成" in raw or "请稍后重试" in raw
    return {
        "http_status": http_status,
        "traces": traces,
        "trace_count": len(traces),
        "final_status": final_status,
        "final_error": final_error,
        "trace_failed": trace_failed,
        "elapsed": round(elapsed, 2),
        "has_incomplete_wording": has_incomplete_wording,
        "raw_tail": raw[-1500:],
        "line_count": line_count,
    }


def main() -> int:
    conversation_id = _create_session()

    print("\n=== Round 1: 我要转账100 ===")
    r1 = _stream_sse(conversation_id, "我要转账100")
    print(f"[R1 summary] http={r1['http_status']} traces={r1['trace_count']} "
          f"lines={r1['line_count']} elapsed={r1['elapsed']}s "
          f"status={r1['final_status']} incomplete={r1['has_incomplete_wording']}")
    if r1["trace_failed"]:
        print(f"[R1 trace_failed] {r1['trace_failed']}")

    # Brief pause to let R1 state settle in BFF repository
    time.sleep(1.0)

    print("\n=== Round 2: 有哪些理财推荐 (should trigger CREATE_TASK switch) ===")
    r2 = _stream_sse(conversation_id, "有哪些理财推荐")
    print(f"[R2 summary] http={r2['http_status']} traces={r2['trace_count']} "
          f"lines={r2['line_count']} elapsed={r2['elapsed']}s "
          f"status={r2['final_status']} incomplete={r2['has_incomplete_wording']}")
    if r2["trace_failed"]:
        print(f"[R2 trace_failed] {r2['trace_failed']}")
    if r2["final_error"]:
        print(f"[R2 error] {str(r2['final_error'])[:300]}")

    print("\n=== Verdict ===")
    plan_d_pass = (
        r2["trace_count"] > 0
        and not r2["has_incomplete_wording"]
        and r2["elapsed"] < 60.0
    )
    print(f"Plan D Step 1 (no incomplete wording, SSE completes): "
          f"{'PASS' if plan_d_pass else 'FAIL'}")
    print(f"R2 final_status={r2['final_status']} trace_failed={r2['trace_failed']}")

    if r2["trace_failed"] or r2["has_incomplete_wording"] or r2["trace_count"] == 0:
        print("\n--- R2 raw tail (last 1500 chars) ---")
        print(r2["raw_tail"])

    return 0 if plan_d_pass else 1


if __name__ == "__main__":
    sys.exit(main())
