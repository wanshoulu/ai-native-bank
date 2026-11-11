# coding: utf-8
"""端到端 SSE 流式场景验证：转账补录中切到理财推荐。

模拟用户报告的故障场景：
  轮1: "我要转账100" → 进入转账补录状态（goals=ccb.transfer.create needs_slots）
  轮2: "有哪些理财推荐" → 应触发意图跳转（CREATE_TASK），SSE 流正常完成

验证点：
  1. decide 日志 switched=True operation=create_task
  2. ContextEngine pool_size > 0（跨轮复用）
  3. SSE 事件流包含 agent.trace / agent.result 事件
  4. 不出现"本次服务未完成"或 stream_error

运行：
  docker exec ai-native-bank-agent-runtime python /app/scripts/test_e2e_sse_intent_switch.py
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from pathlib import Path

import httpx

workspace = Path("/app") if Path("/app/data/ai-native-bank").exists() else Path(__file__).resolve().parents[1]

# 从容器内 runtime-token 文件读取 service token
def _read_token() -> str:
    for p in ("/run/ai-bank-secrets/runtime-token",):
        fp = Path(p)
        if fp.exists():
            return fp.read_text().strip()
    raise RuntimeError("runtime token file not found")

BASE = "http://localhost:18120"
TOKEN = _read_token()

# 最小化 CapabilityProjection（满足 StrictModel 校验）
def _cap(cap_id: str, title: str, description: str, risk: str = "L1") -> dict:
    return {
        "id": cap_id,
        "title": title,
        "description": description,
        "exposure": "model-callable",
        "sideEffect": "read",
        "riskLevel": risk,
        "inputSchema": {"type": "object", "properties": {}},
        "outputSchema": {"type": "object", "properties": {}},
        "possibleErrors": [],
        "readiness": {"status": "ready", "environment": "mock"},
    }

PLANNABLE_CAPS = [
    _cap("bank.transfer.create", "转账汇款", "发起一笔转账汇款", "L2"),
    _cap("bank.wealth.advisory.context.get", "理财建议", "获取个性化理财建议", "L1"),
    _cap("bank.wealth.product.list", "理财产品列表", "查询在售理财产品列表", "L1"),
]
EXECUTABLE_CAPS = [
    _cap("bank.wealth.advisory.context.get", "理财建议", "获取个性化理财建议", "L1"),
    _cap("bank.wealth.product.list", "理财产品列表", "查询在售理财产品列表", "L1"),
]


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {TOKEN}",
        "Accept": "text/event-stream",
        "Content-Type": "application/json",
    }


def _build_request(message: str, conversation_id: str, goals: list | None) -> dict:
    """构造 RuntimeAgentRunRequest。goals 非空时模拟"转账补录"状态。"""
    ctx: dict = {}
    if goals:
        ctx = {
            "pendingEnvelopeId": f"env_{conversation_id}",
            "envelopeRevision": 1,
            "intentShape": "single_intent",
            "goals": goals,
        }
    return {
        "agentRunId": "abr_" + uuid.uuid4().hex[:24],
        "conversationId": conversation_id,
        "requestId": "req_" + uuid.uuid4().hex[:24],
        "traceId": "trace_" + uuid.uuid4().hex[:24],
        "customerId": "customer_e2e_test_001",
        "actorType": "agent",
        "message": message,
        "pageContext": {},
        "conversationContext": ctx,
        "safetyContext": {"recentUserMessages": []},
        "executableCapabilities": EXECUTABLE_CAPS,
        "plannableCapabilities": PLANNABLE_CAPS,
    }


async def _stream_sse(payload: dict, timeout: float = 90.0) -> dict:
    """发起 SSE 请求，收集事件流，返回汇总结果。"""
    events: list[dict] = []
    trace_count = 0
    result_payload: dict | None = None
    state_payload: dict | None = None
    error_seen: str | None = None
    t0 = time.monotonic()

    async with httpx.AsyncClient(timeout=timeout) as client:
        async with client.stream(
            "POST", f"{BASE}/v1/agent-runs",
            json=payload, headers=_headers(),
        ) as resp:
            print(f"  [http] status={resp.status_code} elapsed={time.monotonic()-t0:.2f}s")
            if resp.status_code != 200:
                body = await resp.aread()
                print(f"  [http] non-200 body: {body[:500]}")
                return {"ok": False, "status": resp.status_code, "events": events, "error": body.decode("utf-8", "replace")[:500]}

            event_type = None
            data_lines: list[str] = []
            async for line in resp.aiter_lines():
                if line.startswith("event: "):
                    event_type = line[7:].strip()
                elif line.startswith("data: "):
                    data_lines.append(line[6:])
                elif line == "" and event_type is not None:
                    raw = "\n".join(data_lines)
                    try:
                        parsed = json.loads(raw) if raw else {}
                    except Exception:
                        parsed = {"_raw": raw[:200]}
                    events.append({"event": event_type, "data": parsed})
                    if event_type == "agent.trace":
                        trace_count += 1
                        stage = parsed.get("stage", "")
                        status = parsed.get("status", "")
                        summary = parsed.get("summary", {})
                        title = summary.get("title", "") if isinstance(summary, dict) else ""
                        print(f"  [trace #{trace_count}] stage={stage} status={status} title={title[:60]}")
                        if status == "failed":
                            err = parsed.get("error", {})
                            error_seen = f"trace.failed: {err.get('code','')} {err.get('message','')[:120]}"
                    elif event_type == "agent.result":
                        result_payload = parsed
                        print(f"  [result] status={parsed.get('status','')} agentRunId={parsed.get('agentRunId','')}")
                    elif event_type == "agent.state":
                        state_payload = parsed
                    event_type = None
                    data_lines = []

    elapsed = time.monotonic() - t0
    return {
        "ok": error_seen is None,
        "status": 200,
        "events": events,
        "trace_count": trace_count,
        "result": result_payload,
        "state": state_payload,
        "error": error_seen,
        "elapsed": elapsed,
    }


async def main():
    print(f"[setup] BASE={BASE} token={'set' if TOKEN else 'EMPTY'}")
    print(f"[setup] plannable_caps={[c['id'] for c in PLANNABLE_CAPS]}")

    conversation_id = f"conv-e2e-sse-{uuid.uuid4().hex[:8]}"

    # === 轮 1: 我要转账100（建立转账意图，进入补录状态）===
    print(f"\n=== Round 1: 我要转账100 (conversation_id={conversation_id}) ===")
    r1_payload = _build_request("我要转账100", conversation_id, goals=None)
    r1 = await _stream_sse(r1_payload, timeout=90.0)
    print(f"  [R1 summary] ok={r1['ok']} traces={r1['trace_count']} elapsed={r1['elapsed']:.2f}s error={r1.get('error')}")

    # 从 R1 result/state 提取 goals 构造 R2 的 conversation_context
    r1_goals = None
    if r1.get("state"):
        # agent.state 可能携带 goals（schema 1.0 排除了 goals，但是 result 里可能有）
        pass
    if r1.get("result"):
        # result 里的 conversation_state 可能携带 goals
        cs = r1["result"].get("conversationState") or r1["result"].get("conversation_state")
        if isinstance(cs, dict):
            r1_goals = cs.get("goals")
            if r1_goals:
                print(f"  [R1] extracted goals from result: {[(g.get('intentId'), g.get('status')) for g in r1_goals]}")

    # 如果 R1 没返回 goals，手动构造"转账补录"状态（模拟 BFF 层的 conversation_context）
    if not r1_goals:
        r1_goals = [
            {
                "taskId": "task_transfer_e2e_1",
                "intentId": "ccb.transfer.create",
                "status": "needs_slots",
                "slots": [],
            }
        ]
        print(f"  [R1] using synthesized goals (transfer needs_slots)")

    # === 轮 2: 有哪些理财推荐（应触发意图跳转）===
    print(f"\n=== Round 2: 有哪些理财推荐 (应触发 CREATE_TASK 跳转) ===")
    r2_payload = _build_request("有哪些理财推荐", conversation_id, goals=r1_goals)
    r2 = await _stream_sse(r2_payload, timeout=120.0)
    print(f"  [R2 summary] ok={r2['ok']} traces={r2['trace_count']} elapsed={r2['elapsed']:.2f}s error={r2.get('error')}")

    # === 验证 ===
    print("\n=== VERIFICATION ===")

    # 1. SSE 流是否正常完成
    if r2["trace_count"] > 0 and r2.get("error") is None:
        print(f"[CHECK] PASS - R2 SSE 流正常完成（{r2['trace_count']} traces, elapsed={r2['elapsed']:.2f}s）")
    else:
        print(f"[CHECK] FAIL - R2 SSE 流异常（traces={r2['trace_count']}, error={r2.get('error')})")

    # 2. 是否出现 stream_error
    stream_errors = [e for e in r2["events"] if e["event"] == "agent.trace" and e["data"].get("status") == "failed"]
    if not stream_errors:
        print("[CHECK] PASS - 无 agent.trace failed 事件")
    else:
        for e in stream_errors:
            print(f"[CHECK] FAIL - agent.trace failed: {e['data'].get('error')}")

    # 3. result 是否有正常状态
    if r2.get("result"):
        status = r2["result"].get("status", "")
        if status in ("complete", "clarification", "confirmation", "needs_input"):
            print(f"[CHECK] PASS - R2 result.status={status}")
        elif status == "failed":
            print(f"[CHECK] FAIL - R2 result.status=failed")
        else:
            print(f"[CHECK] INFO - R2 result.status={status}")

    # 4. 检查是否有"本次服务未完成"文案
    all_text = json.dumps(r2["events"], ensure_ascii=False)
    if "本次服务未完成" in all_text or "暂时不可用" in all_text:
        print(f"[CHECK] FAIL - 出现'本次服务未完成'或'暂时不可用'文案")
    else:
        print("[CHECK] PASS - 未出现'本次服务未完成'文案")

    # 5. result.user_response 内容
    if r2.get("result"):
        ur = r2["result"].get("userResponse") or r2["result"].get("userResponses") or []
        if isinstance(ur, list):
            for item in ur:
                if isinstance(item, dict):
                    items = item.get("items", [])
                    print(f"  [R2 user_response] items={len(items)}")
                    for it in items[:3]:
                        if isinstance(it, dict):
                            print(f"    - {it.get('title', it.get('text', str(it)[:80]))}")
        elif isinstance(ur, dict):
            items = ur.get("items", [])
            print(f"  [R2 user_response] items={len(items)}")

    print(f"\n=== SUMMARY ===")
    print(f"R1: ok={r1['ok']} traces={r1['trace_count']} elapsed={r1['elapsed']:.2f}s")
    print(f"R2: ok={r2['ok']} traces={r2['trace_count']} elapsed={r2['elapsed']:.2f}s error={r2.get('error')}")
    print(f"Total events: R1={len(r1['events'])} R2={len(r2['events'])}")


if __name__ == "__main__":
    asyncio.run(main())
