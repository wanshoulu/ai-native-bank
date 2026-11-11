"""Dump remaining key nodes not in first batch."""
import json, os

graph_path = os.path.join(
    os.path.dirname(__file__), '..', '..', '..', '..',
    'data', 'ai-native-bank', 'intent-graph',
    'graph.ccb.intents.v2.1.0.json'
)
graph_path = os.path.abspath(graph_path)

with open(graph_path, 'r', encoding='utf-8') as f:
    graph = json.load(f)

tree = graph.get('intentTree', graph) if isinstance(graph, dict) else graph

target_v4_ids = [
    "ccb.channel.credit.card-manage.query",
    "ccb.channel.credit.card-manage.faq",
    "ccb.channel.credit.card-manage.activation",
    "ccb.channel.credit.card-manage.loss-report",
    "ccb.channel.credit.card-manage.limit-change",
    "ccb.channel.credit.card-manage.transactions.query",
    "ccb.channel.credit.auto-repay.overview",
    "ccb.channel.credit.auto-repay.setup",
    "ccb.channel.credit.bill.overview",
    "ccb.channel.credit.bill.repay",
    "ccb.channel.credit.apply.overview",
    "ccb.channel.credit.apply.apply",
    "ccb.channel.credit.apply.faq",
    "ccb.channel.home.repayment.repay",
]

for node in tree:
    nid = node.get('id', '')
    if nid in target_v4_ids:
        for scene in node.get('scenes', []):
            sd = scene.get('semantic_definition', {})
            rp = scene.get('retrieval_profile', {})
            lc = node.get('lifecycle', 'active')
            print('===', nid, '(lifecycle:', lc, ') ===')
            print('  description:', sd.get('description', ''))
            print('  aliases:', rp.get('aliases', []))
            print('  recallTerms:', rp.get('recallTerms', []))
            print('  negativeExamples:', rp.get('negativeExamples', []))
            print('  confusableIntents:', sd.get('confusableIntents', []))
            break
