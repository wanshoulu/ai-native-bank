"""检查 graph_expand 能否把 ccb.credit.repay.faq 拉进 top5"""
import json, sys
from pathlib import Path
sys.path.insert(0, '.')
from app.config import RecallSettings
from app.v4_graph_adapter import V4GraphAdapter

rs = RecallSettings.from_env()
payload = json.loads(Path(r'd:\project\vscode\data\ai-native-bank\intent-graph\graph.ccb.intents.v2.1.0.json').read_text(encoding='utf-8'))
adapter = V4GraphAdapter(payload, recall_settings=rs)
adapter.apply_recall_settings(rs)

candidates = adapter.recall('还信用卡要手续费吗', page_context={}, limit=10, graph_expand=True)
print('graph_expand=True (默认):')
for i, c in enumerate(candidates[:10], 1):
    exp = ' <-- EXPECTED' if c.intent_id == 'ccb.credit.repay.faq' else ''
    print(f'  [{i}] {c.intent_id}  score={c.score}  title={c.title}{exp}')

print()
print('ccb.home.repayment.faq 的 confusable 邻居:')
for sid in sorted(adapter._confusable_siblings.get('ccb.home.repayment.faq', set())):
    s_int = adapter._intents_by_id.get(sid)
    title = s_int.get('title', '') if s_int else 'NOT FOUND'
    print(f'  - {sid}: {title}')
