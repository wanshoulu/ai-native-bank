"""对比 ccb.credit.repay.faq vs ccb.home.repayment.faq 的召回配置"""
import json, sys
from pathlib import Path
sys.path.insert(0, '.')
from app.config import RecallSettings
from app.v4_graph_adapter import V4GraphAdapter

rs = RecallSettings.from_env()
payload = json.loads(Path(r'd:\project\vscode\data\ai-native-bank\intent-graph\graph.ccb.intents.v2.1.0.json').read_text(encoding='utf-8'))
adapter = V4GraphAdapter(payload, recall_settings=rs)
adapter.apply_recall_settings(rs)

for iid in ['ccb.credit.repay.faq', 'ccb.home.repayment.faq']:
    intent = adapter._intents_by_id.get(iid)
    if not intent:
        continue
    rp = intent.get('_v4_scene', {}).get('retrieval_profile', {})
    print(f'=== {iid} ({intent.get("title")}) ===')
    print(f'  aliases: {rp.get("aliases", [])}')
    print(f'  recallTerms: {rp.get("recallTerms", [])}')
    print(f'  recallTermSets: {rp.get("recallTermSets", [])}')
    print(f'  negativeExamples: {rp.get("negativeExamples", [])}')
    print()
