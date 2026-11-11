"""快速验证多个 single_intent case 的 LLM 裁决效果"""
import sys, time, json
sys.path.insert(0, '.')

from app.v4_graph_adapter import V4GraphAdapter
from app.subgraph_reasoning_v4 import SubgraphReasoningV4
from app.llm_judge_v4 import LLMJudgeV4

import os
adapter = V4GraphAdapter.load(os.path.join(os.path.dirname(__file__), '..', '..', '..', '..', 'data', 'ai-native-bank', 'intent-graph', 'graph.ccb.intents.v2.1.0.json'))
subgraph = SubgraphReasoningV4(adapter)
judge = LLMJudgeV4()
judge._adapter = adapter

dataset = json.loads(open('tests/intent_eval_v4_pilot.json', encoding='utf-8').read())
cases = [c for c in dataset['cases'] if c['intent_shape'] == 'single_intent']
print(f'testing {len(cases)} single_intent cases...')
print('=' * 100)

pass_count = 0
fail_count = 0
skip_count = 0
timeout_count = 0
t_start = time.time()

for i, case in enumerate(cases):
    case_id = case['case_id']
    msg = case['query']
    expected = [e['intent_id'] for e in case.get('expected_intents', [])]

    cands = adapter.recall(msg, page_context={}, limit=12)
    if not cands:
        skip_count += 1
        continue

    top1 = cands[0]
    top2 = cands[1] if len(cands) > 1 else None
    should_judge = top1.score < 80 or (top2 and top2.score >= 60)

    if not should_judge:
        # 不触发 LLM,top1 直接是结果
        passed = top1.intent_id in expected
        if passed:
            pass_count += 1
        else:
            fail_count += 1
            print(f'{case_id:8s} FAIL(no-judge) top1={top1.intent_id} expected={expected} query={msg[:20]}')
        continue

    ctxs = subgraph.build_contexts(cands[:12])
    t0 = time.time()
    result = judge.judge(msg, cands[:12], intent_shape='single_intent', subgraph_contexts=ctxs)
    elapsed = time.time() - t0

    if not result:
        # LLM 超时/失败,回退到词法 top1
        timeout_count += 1
        top1_judged = top1.intent_id
        passed = top1_judged in expected
        if passed:
            pass_count += 1
        else:
            fail_count += 1
            print(f'{case_id:8s} FAIL(timeout {elapsed:.1f}s) top1={top1.intent_id} expected={expected} query={msg[:20]}')
    else:
        # 超强匹配保护:top1 score >= 145 时,LLM 重排后仍保留原 top1
        super_strong = top1.score >= 145
        if super_strong:
            top1_judged = top1.intent_id
        else:
            top1_judged = result[0]['intent_id']
        passed = top1_judged in expected
        if passed:
            pass_count += 1
        else:
            fail_count += 1
            print(f'{case_id:8s} FAIL({elapsed:.1f}s) judged={top1_judged} expected={expected} query={msg[:20]}')
            print(f'         cands top5: {[(c.intent_id, c.score) for c in cands[:5]]}')

    # 进度
    if (i + 1) % 20 == 0:
        elapsed_total = time.time() - t_start
        print(f'  progress: {i+1}/{len(cases)} ({elapsed_total:.1f}s) pass={pass_count} fail={fail_count} timeout={timeout_count}')

elapsed_total = time.time() - t_start
print('=' * 100)
print(f'TOTAL: {len(cases)} cases, pass={pass_count}, fail={fail_count}, skip={skip_count}, timeout={timeout_count}, elapsed={elapsed_total:.1f}s')
print(f'PASS rate: {pass_count}/{len(cases)-skip_count} = {pass_count/(len(cases)-skip_count)*100:.1f}%')
