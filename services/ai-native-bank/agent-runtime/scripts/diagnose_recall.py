"""快速诊断 _tugraph_recall 为何对'我想申请信用卡'返回0候选"""
import sys, os
sys.path.insert(0, "/app")
os.environ.setdefault("AI_BANK_TUGRAPH_URI", "bolt://host.docker.internal:7687")
os.environ.setdefault("AI_BANK_TUGRAPH_USER", "admin")
os.environ.setdefault("AI_BANK_TUGRAPH_PASSWORD", "73@TuGraph")
os.environ.setdefault("AI_BANK_TUGRAPH_REST_URL", "http://host.docker.internal:7070")

from pathlib import Path
from app.config import RuntimeSettings
from app.tugraph_source import load_v4_payload_from_tugraph
from app.v4_graph_adapter import V4GraphAdapter
from app.semantic_recall_v4 import SemanticRecallV4

settings = RuntimeSettings.from_env()
payload = load_v4_payload_from_tugraph(settings)
adapter = V4GraphAdapter(payload, Path("."))

tugraph_config = {
    "uri": settings.tugraph_uri,
    "user": settings.tugraph_user,
    "password": settings.tugraph_password,
    "database": settings.tugraph_database,
    "base_url": getattr(settings, "tugraph_rest_url", "http://host.docker.internal:7070"),
}
recall = SemanticRecallV4(adapter, tugraph_config=tugraph_config)
recall.prewarm()

print(f"tugraph_enabled: {recall._tugraph_enabled}")
print(f"threshold: {recall._threshold}")
print(f"_intents_by_v4_node_id count: {len(recall._intents_by_v4_node_id)}")

# 检查 ccb.channel.credit.apply.apply 是否在映射中
test_v4_ids = [
    "ccb.channel.credit.apply.faq",
    "ccb.channel.credit.apply.apply",
    "ccb.channel.credit.apply.overview",
]
for v4_id in test_v4_ids:
    intent = recall._intents_by_v4_node_id.get(v4_id)
    if intent:
        print(f"  {v4_id} -> intent_id={intent.get('id')}, title={intent.get('title', '')[:30]}")
    else:
        print(f"  {v4_id} -> NOT FOUND in _intents_by_v4_node_id")

# 直接测试 _tugraph_knn_search
print("\n--- _tugraph_knn_search test ---")
query_vec = recall._embed(["我想申请信用卡"])[0]
knn_results = recall._tugraph_knn_search(query_vec, limit=10)
print(f"KNN results: {len(knn_results)}")
for v4_id, sim in knn_results[:5]:
    print(f"  v4_id={v4_id}, sim={sim:.4f}, in_mapping={v4_id in recall._intents_by_v4_node_id}")

# 直接测试 _tugraph_recall
print("\n--- _tugraph_recall test ---")
candidates = recall._tugraph_recall("我想申请信用卡", exclude_ids=set())
print(f"Candidates: {len(candidates)}")
for c in candidates[:5]:
    print(f"  intent={c.get('intentId', '')}, score={c.get('score', 0)}, evidence={c.get('evidence', [])}")

# 也检查 exclude 是否有问题
print("\n--- _tugraph_recall with empty exclude ---")
candidates2 = recall._tugraph_recall("我想申请信用卡")
print(f"Candidates (no exclude): {len(candidates2)}")
