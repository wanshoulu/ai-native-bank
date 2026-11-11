"""检查 graph_expand 扩召了哪些候选"""
import json, sys
from pathlib import Path
sys.path.insert(0, '.')
from app.config import RecallSettings
from app.v4_graph_adapter import V4GraphAdapter

rs = RecallSettings.from_env()
payload = json.loads(Path(r'd:\project\vscode\data\ai-native-bank\intent-graph\graph.ccb.intents.v2.1.0.json').read_text(encoding='utf-8'))
adapter = V4GraphAdapter(payload, recall_settings=rs)
adapter.apply_recall_settings(rs)

# 不带 graph_expand
print('graph_expand=False:')
candidates_no_expand = adapter.recall('还信用卡要手续费吗', page_context={}, limit=10, graph_expand=False)
for i, c in enumerate(candidates_no_expand[:10], 1):
    exp = ' <-- EXPECTED' if c.intent_id == 'ccb.credit.repay.faq' else ''
    print(f'  [{i}] {c.intent_id}  score={c.score}  title={c.title}{exp}')

print()
print('graph_expand=True (默认):')
candidates = adapter.recall('还信用卡要手续费吗', page_context={}, limit=10, graph_expand=True)
for i, c in enumerate(candidates[:10], 1):
    exp = ' <-- EXPECTED' if c.intent_id == 'ccb.credit.repay.faq' else ''
    evidence = c.evidence if hasattr(c, 'evidence') else []
    is_expand = any('graph_expand' in str(e) for e in (evidence or []))
    mark = ' [扩召]' if is_expand else ''
    print(f'  [{i}] {c.intent_id}  score={c.score}  title={c.title}{exp}{mark}')

# 看 evidence
print()
print('扩召候选 evidence:')
for c in candidates:
    if hasattr(c, 'evidence') and c.evidence:
        for e in c.evidence:
            if 'graph_expand' in str(e):
                print(f'  {c.intent_id} (score={c.score}): {e}')
                break
