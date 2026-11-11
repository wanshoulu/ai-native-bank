"""端到端测试 TuGraph KNN 语义召回"""
import requests, time

BASE = "http://localhost:18120"
TOKEN = "gPytexfeVYD2zHN-0pHSSpkmGztD5mTGIQAgrKdFg0U"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}

# 等待容器就绪
time.sleep(3)
r = requests.get(f"{BASE}/health", timeout=10)
health = r.json()
print(f"Health: ready={health.get('ready')} state={health.get('runtimeState')}")

messages = [
    "我想申请信用卡",
    "怎么查余额",
    "信用卡额度不够用",
    "帮我转账",
    "理财产品收益怎么样",
]

for msg in messages:
    r = requests.get(
        f"{BASE}/v1/internal/intent-recall",
        params={"message": msg},
        headers=HEADERS,
        timeout=30,
    )
    print(f"\n--- '{msg}' (status={r.status_code}) ---")
    if r.status_code != 200:
        print(f"  Error: {r.text[:200]}")
        continue
    data = r.json()
    candidates = data.get("candidates", [])
    print(f"  Candidates: {len(candidates)}")
    for c in candidates[:5]:
        intent_id = c.get("intentId", "")
        score = c.get("score", 0)
        evidence = c.get("evidence", [])
        title = c.get("title", "")
        print(f"  [{score:>3}] {intent_id[:55]}")
        print(f"        title={title[:40]} evidence={evidence}")
