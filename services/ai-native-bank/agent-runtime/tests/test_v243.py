"""验证 V4-243 修复效果"""
import sys, time
sys.path.insert(0, '.')

from app.v4_graph_adapter import V4GraphAdapter
from app.subgraph_reasoning_v4 import SubgraphReasoningV4
from app.llm_judge_v4 import LLMJudgeV4

import os
adapter = V4GraphAdapter.load(os.path.join(os.path.dirname(__file__), '..', '..', '..', '..', 'data', 'ai-native-bank', 'intent-graph', 'graph.ccb.intents.v2.1.0.json'))
subgraph = SubgraphReasoningV4(adapter)
judge = LLMJudgeV4()
judge._adapter = adapter

msg = '还信用卡要手续费吗'
cands = adapter.recall(msg, page_context={}, limit=12)
print(f'cands count={len(cands)}')
for i, c in enumerate(cands):
    mark = ' <== EXPECTED' if c.intent_id == 'ccb.credit.repay.faq' else ''
    print(f'  {i+1}. {c.intent_id:45s} score={c.score:4d}{mark}')

ctxs = subgraph.build_contexts(cands[:12])
t0 = time.time()
result = judge.judge(msg, cands[:12], intent_shape='single_intent', subgraph_contexts=ctxs)
elapsed = time.time() - t0
print(f'\nLLM judge ({elapsed:.2f}s):')
for r in result:
    print(f'  {r["intent_id"]} conf={r["confidence"]}')

expected = 'ccb.credit.repay.faq'
top1_judged = result[0]['intent_id'] if result else '(empty)'
print(f'\nexpected: {expected}')
print(f'top1_judged: {top1_judged}')
print('PASS' if top1_judged == expected else 'FAIL')
