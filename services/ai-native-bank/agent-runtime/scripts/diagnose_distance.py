"""验证 L2 vs cosine 距离问题，并计算真实 cosine 相似度"""
import sys, os, json, requests, math
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

# 生成查询 embedding
test_msg = "我想申请信用卡"
query_vec = recall._embed([test_msg])[0]
print(f"Query embedding: dim={len(query_vec)}")

# 从 TuGraph 获取一个已知 intent 的 embedding
# 登录 TuGraph
resp = requests.post(
    "http://host.docker.internal:7070/login",
    json={"user": "admin", "password": "73@TuGraph"},
    headers={"Content-Type": "application/json"},
    timeout=10,
)
token = resp.json()["jwt"]
headers = {"Content-Type": "application/json", "Authorization": f"Bearer {token}"}

# 获取 ccb.channel.credit.apply.apply 的 embedding
cypher = "MATCH (n:IntentNode {id: 'ccb.channel.credit.apply.apply'}) RETURN n.embedding AS emb"
r = requests.post(
    "http://host.docker.internal:7070/cypher",
    json={"script": cypher, "graph": "default"},
    headers=headers,
    timeout=30,
)
data = r.json()
rows = data.get("result", [])
if rows and rows[0]:
    stored_emb = rows[0][0]
    print(f"Stored embedding: dim={len(stored_emb)}")

    # 计算 L2 距离
    l2_dist = math.sqrt(sum((a - b) ** 2 for a, b in zip(query_vec, stored_emb)))
    print(f"L2 distance: {l2_dist:.4f}")
    print(f"1 - L2 = {1 - l2_dist:.4f}  (当前代码的 similarity)")

    # 计算 cosine 相似度
    dot = sum(a * b for a, b in zip(query_vec, stored_emb))
    norm_q = math.sqrt(sum(a * a for a in query_vec))
    norm_s = math.sqrt(sum(a * a for a in stored_emb))
    cosine_sim = dot / (norm_q * norm_s) if norm_q > 0 and norm_s > 0 else 0
    print(f"Cosine similarity: {cosine_sim:.4f}")
    print(f"Cosine distance: {1 - cosine_sim:.4f}")

    # 验证: 对于归一化向量, L2^2 = 2*(1-cos_sim)
    print(f"\n验证: L2^2/2 = {l2_dist**2/2:.4f}, 1-cosine_sim = {1-cosine_sim:.4f}")

    # 检查向量是否归一化
    print(f"Query norm: {norm_q:.4f}")
    print(f"Stored norm: {norm_s:.4f}")

    # 检查前10个值
    print(f"\nQuery vec[:10]:  {[round(x, 4) for x in query_vec[:10]]}")
    print(f"Stored vec[:10]: {[round(x, 4) for x in stored_emb[:10]]}")
    print(f"Query vec type: {type(query_vec[0])}, Stored vec type: {type(stored_emb[0])}")

    # 检查stored_emb是否有异常值
    zero_count = sum(1 for x in stored_emb if x == 0.0)
    print(f"Stored vec zero count: {zero_count}/{len(stored_emb)}")
    print(f"Stored vec min={min(stored_emb):.6f}, max={max(stored_emb):.6f}")

    # 尝试：直接用相同的查询文本生成embedding，看是否与query_vec一致
    test_vec2 = recall._embed(["我想申请信用卡"])[0]
    dot_self = sum(a * b for a, b in zip(query_vec, test_vec2))
    norm_self = math.sqrt(sum(a * a for a in test_vec2))
    cos_self = dot_self / (norm_q * norm_self) if norm_q > 0 and norm_self > 0 else 0
    print(f"\n自一致性测试 (相同文本两次embedding): cosine_sim={cos_self:.6f}")
else:
    print("未找到 embedding")

# 也检查 KNN 搜索返回的实际 distance 值
print(f"\n--- KNN 搜索 distance vs 手动计算 ---")
knn_results = recall._tugraph_knn_search(query_vec, limit=5)
for v4_id, sim in knn_results[:3]:
    print(f"  v4_id={v4_id}")
    print(f"    code similarity (1-dist): {sim:.4f}")

    # 获取该节点的 embedding 并手动计算
    cypher2 = f"MATCH (n:IntentNode {{id: '{v4_id}'}}) RETURN n.embedding AS emb"
    r2 = requests.post(
        "http://host.docker.internal:7070/cypher",
        json={"script": cypher2, "graph": "default"},
        headers=headers,
        timeout=30,
    )
    rows2 = r2.json().get("result", [])
    if rows2 and rows2[0]:
        emb2 = rows2[0][0]
        l2 = math.sqrt(sum((a - b) ** 2 for a, b in zip(query_vec, emb2)))
        dot2 = sum(a * b for a, b in zip(query_vec, emb2))
        norm2 = math.sqrt(sum(a * a for a in emb2))
        cos = dot2 / (norm_q * norm2) if norm_q > 0 and norm2 > 0 else 0
        print(f"    manual L2: {l2:.4f}, 1-L2: {1-l2:.4f}")
        print(f"    manual cosine_sim: {cos:.4f}")
