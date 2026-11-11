"""诊断 TuGraph KNN 搜索和 intent 查找"""
import sys, os, json, requests, time
from pathlib import Path

sys.path.insert(0, "/app")
os.environ.setdefault("AI_BANK_TUGRAPH_URI", "bolt://host.docker.internal:7687")
os.environ.setdefault("AI_BANK_TUGRAPH_USER", "admin")
os.environ.setdefault("AI_BANK_TUGRAPH_PASSWORD", "73@TuGraph")
os.environ.setdefault("AI_BANK_TUGRAPH_REST_URL", "http://host.docker.internal:7070")

from app.config import RuntimeSettings
from app.tugraph_source import load_v4_payload_from_tugraph
from app.v4_graph_adapter import V4GraphAdapter
from app.semantic_recall_v4 import SemanticRecallV4

settings = RuntimeSettings.from_env()
print(f"Settings: tugraph_uri={settings.tugraph_uri}")
print(f"  model_base_url={settings.model_base_url}")
print(f"  model_api_key={'***' + settings.model_api_key[-5:] if settings.model_api_key else 'EMPTY'}")

# 加载图谱
payload = load_v4_payload_from_tugraph(settings)
adapter = V4GraphAdapter(payload, Path("."))
print(f"\n图谱: {adapter.graph_id} v{adapter.version}, {len(adapter._intents)} intents")

# 检查 intent 的 _v4_node_id
sample = adapter._intents[0]
print(f"Sample intent: id={sample.get('id')} _v4_node_id={sample.get('_v4_node_id', 'NOT SET')}")

# 初始化 SemanticRecallV4（正确签名）
tugraph_config = {
    "uri": settings.tugraph_uri,
    "user": settings.tugraph_user,
    "password": settings.tugraph_password,
    "database": settings.tugraph_database,
    "base_url": getattr(settings, "tugraph_rest_url", "http://host.docker.internal:7070"),
}
recall = SemanticRecallV4(adapter, tugraph_config=tugraph_config)
recall.prewarm()
print(f"\nSemanticRecallV4: threshold={recall._threshold} tugraph_enabled={recall._tugraph_enabled}")
print(f"  _intents_by_v4_node_id: {len(recall._intents_by_v4_node_id)} entries")
if recall._intents_by_v4_node_id:
    sample_v4 = list(recall._intents_by_v4_node_id.keys())[0]
    print(f"  sample v4_node_id: {sample_v4}")

# 测试 embedding 生成
test_msg = "我想申请信用卡"
try:
    query_vec = recall._embed([test_msg])[0]
    print(f"\nEmbedding for '{test_msg}': dim={len(query_vec)}, first3={query_vec[:3]}")
except Exception as e:
    print(f"\nEmbedding FAILED: {e}")
    import traceback; traceback.print_exc()
    sys.exit(1)

# 测试 KNN 搜索
try:
    knn_results = recall._tugraph_knn_search(query_vec, limit=10)
    print(f"\nKNN results: {len(knn_results)}")
    for v4_id, sim in knn_results[:5]:
        print(f"  v4_id={v4_id} sim={sim:.4f}")
        found = v4_id in recall._intents_by_v4_node_id
        found2 = v4_id in adapter._intents_by_id
        print(f"    in _intents_by_v4_node_id: {found}")
        print(f"    in _intents_by_id: {found2}")
        if found:
            intent = recall._intents_by_v4_node_id[v4_id]
            print(f"    intent_id={intent.get('id')}")
        elif found2:
            intent = adapter._intents_by_id[v4_id]
            print(f"    intent_id={intent.get('id')}")
except Exception as e:
    print(f"\nKNN search FAILED: {e}")
    import traceback; traceback.print_exc()

# 测试完整召回
print(f"\n--- Full recall test ---")
candidates = recall.recall(test_msg)
print(f"Candidates: {len(candidates)}")
for c in candidates[:5]:
    print(f"  [{c.get('score')}] {c.get('intentId')} evidence={c.get('evidence')}")
