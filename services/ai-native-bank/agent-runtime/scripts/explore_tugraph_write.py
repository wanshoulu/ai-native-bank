#!/usr/bin/env python
"""探索 TuGraph 向量写入方式。"""
from neo4j import GraphDatabase

URI = "bolt://localhost:7687"
USER = "admin"
PASSWORD = "73@TuGraph"
DB = "default"

driver = GraphDatabase.driver(URI, auth=(USER, PASSWORD))
session = driver.session(database=DB)

# 1. 查询所有写相关的过程
print("=== 写入相关过程 ===")
try:
    r = session.run(
        "CALL dbms.procedures() YIELD name, signature "
        "WHERE name STARTS WITH 'db.' AND (name CONTAINS 'Vertex' OR name CONTAINS 'upsert' OR name CONTAINS 'update' OR name CONTAINS 'write' OR name CONTAINS 'import') "
        "RETURN name, signature"
    )
    for record in r:
        print(f"  {record['name']}")
        print(f"    {record['signature']}")
except Exception as e:
    print(f"查询失败: {e}")

# 2. 查看向量索引
print("\n=== 向量索引 ===")
try:
    r = session.run("CALL db.showVertexVectorIndex() YIELD * RETURN *")
    for record in r:
        print(f"  {dict(record)}")
except Exception as e:
    print(f"查询失败: {e}")

# 3. 尝试 Cypher SET 写入（再试一次确认是否真的不支持）
print("\n=== 尝试 Cypher SET 写入 ===")
try:
    session.run(
        "MATCH (n:IntentNode {id: 'root'}) SET n.embedding = $vec",
        vec=[0.1] * 1024,
    )
    print("  SET 写入成功！")
    # 验证
    r = session.run("MATCH (n:IntentNode {id: 'root'}) RETURN n.embedding AS emb")
    rec = r.single()
    if rec and rec['emb']:
        print(f"  验证: embedding长度={len(rec['emb'])}, 前5个值={rec['emb'][:5]}")
except Exception as e:
    print(f"  SET 写入失败: {e}")

# 4. 尝试 db.upsertVertex
print("\n=== 尝试 db.upsertVertex ===")
try:
    r = session.run(
        "CALL db.upsertVertex('IntentNode', {id: 'root', embedding: $vec})",
        vec=[0.1] * 1024,
    )
    print(f"  upsertVertex 成功: {dict(r.single()) if r.peek() else 'no result'}")
except Exception as e:
    print(f"  upsertVertex 失败: {e}")

# 5. 查询所有包含 embedding 的过程
print("\n=== 查找可用写入方式 ===")
try:
    r = session.run(
        "CALL dbms.procedures() YIELD name, signature "
        "WHERE name CONTAINS 'upsert' OR name CONTAINS 'Vertex' "
        "RETURN name, signature "
        "LIMIT 20"
    )
    for record in r:
        print(f"  {record['name']}: {record['signature']}")
except Exception as e:
    print(f"查询失败: {e}")

driver.close()
