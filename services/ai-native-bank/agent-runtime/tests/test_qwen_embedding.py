"""测试 Qwen embedding API 连通性"""
import os, json, urllib.request

# 加载 .env
env_path = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "..", ".env")
if not os.path.exists(env_path):
    env_path = r"d:\project\vscode\.env"
with open(env_path, encoding="utf-8") as f:
    for line in f:
        line = line.strip()
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            os.environ[k.strip()] = v.strip()

api_key = os.getenv("QWEN_KEY", "")
print(f"QWEN_KEY: {api_key[:15]}...")

# Qwen embedding API (DashScope compatible mode)
url = "https://dashscope.aliyuncs.com/compatible-mode/v1/embeddings"
payload = json.dumps({
    "model": "text-embedding-v4",
    "input": ["查信用卡还款", "看看还欠"],
    "dimensions": 1024,
}).encode()
req = urllib.request.Request(url, data=payload, headers={
    "Authorization": f"Bearer {api_key}",
    "Content-Type": "application/json",
})
try:
    resp = urllib.request.urlopen(req, timeout=10)
    data = json.loads(resp.read())
    embeddings = data.get("data", [])
    print(f"Success! Got {len(embeddings)} embeddings")
    if embeddings:
        v = embeddings[0].get("embedding", [])
        print(f"  dim={len(v)}, first5={v[:5]}")
    # cosine similarity
    if len(embeddings) == 2:
        v1 = embeddings[0]["embedding"]
        v2 = embeddings[1]["embedding"]
        import math
        dot = sum(a*b for a,b in zip(v1,v2))
        n1 = math.sqrt(sum(a*a for a in v1))
        n2 = math.sqrt(sum(b*b for b in v2))
        sim = dot / (n1 * n2)
        print(f"  cosine(查信用卡还款, 看看还欠) = {sim:.4f}")
except Exception as e:
    print(f"FAILED: {e}")
