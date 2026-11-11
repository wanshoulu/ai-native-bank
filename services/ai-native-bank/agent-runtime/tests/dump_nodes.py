"""Dump current retrieval data for key nodes involved in failures."""
import json, sys, os

graph_path = os.path.join(
    os.path.dirname(__file__), '..', '..', '..', '..',
    'data', 'ai-native-bank', 'intent-graph',
    'graph.ccb.intents.v2.1.0.json'
)
graph_path = os.path.abspath(graph_path)

with open(graph_path, 'r', encoding='utf-8') as f:
    graph = json.load(f)

tree = graph.get('intentTree', graph) if isinstance(graph, dict) else graph

# V4 IDs of all nodes involved in failures
target_v4_ids = [
    # zero recall targets
    "ccb.channel.credit.repay.overview",
    "ccb.channel.credit.card-manage.transactions.query",
    "ccb.channel.credit.card-manage.loss-report",
    "ccb.channel.credit.installment.overview",
    "ccb.channel.credit.installment.apply",
    "ccb.channel.home.details.spending-explain",
    "ccb.channel.home.details.query",
    # top1 wrong - correct targets
    "ccb.channel.credit.cash.apply",
    "ccb.channel.credit.cash.overview",
    "ccb.channel.home.account.apply",
    "ccb.channel.home.account.faq",
    "ccb.channel.home.account.query",
    "ccb.channel.credit.card-manage.query",
    "ccb.channel.credit.card-manage.limit-change",
    "ccb.channel.credit.bill.overview",
    "ccb.channel.credit.auto-repay.overview",
    "ccb.channel.credit.auto-repay.setup",
    "ccb.channel.home.loan.repayment",
    "ccb.channel.home.repayment.overview",
    "ccb.channel.home.repayment.faq",
    "ccb.channel.credit.repay.repay",
    "ccb.channel.credit.repay.faq",
    # missing confusable targets
    "ccb.channel.credit.limit.overview",
    "ccb.channel.credit.points.overview",
    "ccb.channel.credit.bill.repay",
    "ccb.channel.credit.apply.overview",
    "ccb.channel.credit.apply.apply",
    "ccb.channel.credit.apply.faq",
    "ccb.channel.credit.card-manage.activation",
    "ccb.channel.home.transfer.query",
    "ccb.channel.me.cards.faq",
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
