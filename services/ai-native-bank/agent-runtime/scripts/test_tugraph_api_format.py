"""测试 TuGraph REST API 响应格式"""
import requests, json

# 登录
resp = requests.post(
    "http://host.docker.internal:7070/login",
    json={"user": "admin", "password": "73@TuGraph"},
    headers={"Content-Type": "application/json"},
    timeout=10,
)
token = resp.json()["jwt"]
headers = {"Content-Type": "application/json", "Authorization": f"Bearer {token}"}

# 简单 Cypher 查询
cypher = "MATCH (n:IntentNode) WHERE n.embedding IS NOT NULL RETURN count(n) AS cnt LIMIT 1"
r = requests.post(
    "http://host.docker.internal:7070/cypher",
    json={"script": cypher, "graph": "default"},
    headers=headers,
    timeout=30,
)
print(f"Status: {r.status_code}")
resp_data = r.json()
print(f"Response keys: {list(resp_data.keys())}")
print(f"Full response: {json.dumps(resp_data, ensure_ascii=False)[:500]}")

# 检查 'data' vs 'result' key
print(f"\nresp_data.get('data') = {resp_data.get('data', 'NOT FOUND')}")
print(f"resp_data.get('result') = {resp_data.get('result', 'NOT FOUND')}")

# KNN 搜索测试 - 用一个测试向量
cypher_knn = (
    "CALL db.vertexVectorKnnSearch('IntentNode', 'embedding', "
    "[0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0, "
    "0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0, "
    "0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0, "
    "0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0] + "
    "[0.0]*984, {limit: 3}) YIELD node, distance "
    "RETURN node.id AS node_id, distance"
)
r2 = requests.post(
    "http://host.docker.internal:7070/cypher",
    json={"script": cypher_knn, "graph": "default"},
    headers=headers,
    timeout=30,
)
print(f"\nKNN Status: {r2.status_code}")
resp2 = r2.json()
print(f"KNN Response keys: {list(resp2.keys())}")
print(f"KNN resp_data.get('data') = {resp2.get('data', 'NOT FOUND')}")
print(f"KNN resp_data.get('result') = {resp2.get('result', 'NOT FOUND')}")
print(f"KNN Full response: {json.dumps(resp2, ensure_ascii=False)[:500]}")
