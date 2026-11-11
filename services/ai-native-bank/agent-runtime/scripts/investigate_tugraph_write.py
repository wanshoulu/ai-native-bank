#!/usr/bin/env python
"""调查 TuGraph embedding 写入问题。"""
import json
from neo4j import GraphDatabase

URI = "bolt://localhost:7687"
USER = "admin"
PASSWORD = "73@TuGraph"
DB = "default"

driver = GraphDatabase.driver(URI, auth=(USER, PASSWORD))
session = driver.session(database=DB)

# 1. 查看一个 IntentNode 的完整数据
print("=== 查看 IntentNode 样本 ===")
r = session.run("MATCH (n:IntentNode) RETURN n LIMIT 1")
for rec in r:
    node = rec["n"]
    print(f"  labels: {node.labels}")
    for k, v in node.items():
        val_str = str(v)[:100]
        print(f"  {k}: {val_str}")

# 2. 查看一个节点的id字段
print("\n=== 查看 IntentNode id 字段 ===")
r = session.run("MATCH (n:IntentNode) RETURN n.id AS id LIMIT 3")
for rec in r:
    print(f"  id: {rec['id']}")

# 3. 尝试 Cypher SET 写入（重启后可能支持了）
print("\n=== 尝试 Cypher SET 写入 ===")
test_vec = [0.1] * 1024
try:
    r = session.run(
        "MATCH (n:IntentNode {id: 'root'}) SET n.embedding = $vec RETURN n.embedding AS emb",
        vec=test_vec,
    )
    rec = r.single()
    if rec and rec['emb']:
        print(f"  SET 写入成功！embedding 长度: {len(rec['emb'])}")
    else:
        print("  SET 写入返回空")
except Exception as e:
    print(f"  SET 写入失败: {e}")

# 4. 尝试 db.upsertVertexByJson 写入
print("\n=== 尝试 db.upsertVertexByJson 写入 ===")
try:
    data = json.dumps([{"id": "root", "embedding": test_vec}])
    r = session.run(
        "CALL db.upsertVertexByJson('IntentNode', $data) YIELD total, data_error, insert, update "
        "RETURN total, data_error, insert, update",
        data=data,
    )
    rec = r.single()
    print(f"  total={rec['total']}, data_error={rec['data_error']}, insert={rec['insert']}, update={rec['update']}")
except Exception as e:
    print(f"  upsertVertexByJson 失败: {e}")

# 5. 验证写入结果
print("\n=== 验证写入结果 ===")
try:
    r = session.run("MATCH (n:IntentNode {id: 'root'}) RETURN n.embedding AS emb")
    rec = r.single()
    if rec and rec['emb']:
        print(f"  root 节点 embedding 长度: {len(rec['emb'])}, 前3个值: {rec['emb'][:3]}")
    else:
        print("  root 节点 embedding 为空")
except Exception as e:
    print(f"  验证失败: {e}")

# 6. 尝试 db.upsertVertex (非Json版本)
print("\n=== 尝试 db.upsertVertex 写入 ===")
try:
    data = json.dumps([{"id": "root", "embedding": test_vec}])
    r = session.run(
        "CALL db.upsertVertex('IntentNode', $data) YIELD total, data_error, insert, update "
        "RETURN total, data_error, insert, update",
        data=data,
    )
    rec = r.single()
    print(f"  total={rec['total']}, data_error={rec['data_error']}, insert={rec['insert']}, update={rec['update']}")
except Exception as e:
    print(f"  upsertVertex 失败: {e}")

# 7. 验证
print("\n=== 再次验证 ===")
try:
    r = session.run("MATCH (n:IntentNode {id: 'root'}) RETURN n.embedding AS emb")
    rec = r.single()
    if rec and rec['emb']:
        print(f"  root 节点 embedding 长度: {len(rec['emb'])}, 前3个值: {rec['emb'][:3]}")
    else:
        print("  root 节点 embedding 为空")
except Exception as e:
    print(f"  验证失败: {e}")

# 8. 查看 IntentNode schema（通过 Cypher 查看属性键）
print("\n=== 查看所有属性键 ===")
try:
    r = session.run("MATCH (n:IntentNode) UNWIND keys(n) AS key RETURN DISTINCT key ORDER BY key")
    for rec in r:
        print(f"  {rec['key']}")
except Exception as e:
    print(f"  查看属性键失败: {e}")

driver.close()
