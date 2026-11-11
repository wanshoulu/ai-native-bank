#!/usr/bin/env python
"""Test multi-intent recognition for '查余额，给张三转100' through the full constraint pipeline."""
import sys, os, json, asyncio
sys.path.insert(0, "/app")
os.environ.setdefault("AI_BANK_TUGRAPH_URI", "bolt://host.docker.internal:7687")
os.environ.setdefault("AI_BANK_TUGRAPH_USER", "admin")
os.environ.setdefault("AI_BANK_TUGRAPH_PASSWORD", "73@TuGraph")

from app.config import RuntimeSettings
from app.tugraph_source import load_v4_payload_from_tugraph
from app.v4_graph_adapter import V4GraphAdapter
from app.intent_shape import analyze_intent_shape
from app.service import recall_intent_constraints, _EvalProbeRequest, _EvalProbeCapability

settings = RuntimeSettings.from_env()
payload = load_v4_payload_from_tugraph(settings)

print("=== 1. INTENT SHAPE ===")
shape = analyze_intent_shape("查余额，给张三转100")
print(f"shape={shape.shape}")
print(f"authoritative={shape.authoritative}")
print(f"segments={shape.segments}")
print(f"expected_task_count={shape.expected_task_count}")
print(f"evidence={shape.evidence}")

print("\n=== 2. V4GraphAdapter recall ===")
adapter = V4GraphAdapter(payload, __import__("pathlib").Path("."))
candidates = adapter.recall("查余额，给张三转100", page_context={})
print(f"Candidates count: {len(candidates)}")
for c in candidates[:8]:
    print(f"  intentId={c.intent_id} score={c.score} evidence={c.evidence}")

print("\n=== 3. recall_intent_constraints ===")
request = _EvalProbeRequest(
    message="查余额，给张三转100",
    page_context={},
    plannable_capabilities=[_EvalProbeCapability(id=item) for item in adapter.all_capability_ids],
)
constraints = recall_intent_constraints(request, adapter, None, shape)
print(f"Constraints count: {len(constraints)}")
for c in constraints:
    print(f"  intentId={c.intent_id}")
    print(f"    required_shape={c.required_shape}")
    print(f"    expected_task_count={c.expected_task_count}")
    print(f"    authoritative={c.authoritative}")
    print(f"    required={c.required}")
    print(f"    score={c.score}")
    print(f"    evidence={c.evidence}")
    print(f"    constraint={c.constraint}")
    print(f"    source={c.source}")

print("\n=== 4. SHAPE vs CONSTRAINTS ALIGNMENT ===")
if constraints:
    c0 = constraints[0]
    if c0.required_shape == "multi_intent" and c0.expected_task_count == 2:
        print("PASS: multi_intent shape with expected_task_count=2 propagated to constraints")
    else:
        print(f"FAIL: required_shape={c0.required_shape}, expected_task_count={c0.expected_task_count}")
else:
    print("FAIL: no constraints returned")

print("---DONE---")
