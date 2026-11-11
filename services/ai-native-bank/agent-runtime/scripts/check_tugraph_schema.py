#!/usr/bin/env python
"""检查 TuGraph IntentNode schema。"""
from neo4j import GraphDatabase

URI = "bolt://localhost:7687"
USER = "admin"
PASSWORD = "73@TuGraph"
DB = "default"

driver = GraphDatabase.driver(URI, auth=(USER, PASSWORD))
session = driver.session(database=DB)

# 1. 用不同方式查看 schema
print("=== db.getVertexSchema ===")
for yield_clause in ["YIELD schema RETURN schema", "YIELD * RETURN *", ""]:
    try:
        cypher = f"CALL db.getVertexSchema('IntentNode') {yield_clause}".strip()
        r = session.run(cypher)
        for rec in r:
            print(f"  [{yield_clause or 'no-yield'}]: {dict(rec)}")
        break
    except Exception as e:
        print(f"  [{yield_clause or 'no-yield'}] 失败: {e}")

# 2. 查看 alterLabelAddFields 的详细签名
print("\n=== db.alterLabelAddFields 签名 ===")
try:
    r = session.run(
        "CALL dbms.procedures() YIELD name, signature "
        "WHERE name = 'db.alterLabelAddFields' "
        "RETURN name, signature"
    )
    for rec in r:
        print(f"  {rec['name']}: {rec['signature']}")
except Exception as e:
    print(f"  失败: {e}")

# 3. 用不同格式尝试 alterLabelAddFields
print("\n=== 尝试不同格式添加字段 ===")
formats = [
    # 格式A: type 用 FLOAT
    "CALL db.alterLabelAddFields('IntentNode', [{name: 'embedding', type: 'FLOAT', optional: true, dimension: 1024}])",
    # 格式B: type 用 FLOAT[]
    "CALL db.alterLabelAddFields('IntentNode', [{name: 'embedding2', type: 'FLOAT[]', optional: true}])",
    # 格式C: type 用 string
    "CALL db.alterLabelAddFields('IntentNode', [{name: 'embedding3', type: 'string', optional: true}])",
    # 格式D: 不带 dimension
    "CALL db.alterLabelAddFields('IntentNode', [{name: 'embedding4', type: 'FLOAT', optional: true}])",
]
for i, cypher in enumerate(formats):
    try:
        session.run(cypher)
        print(f"  格式{chr(65+i)} 成功")
    except Exception as e:
        print(f"  格式{chr(65+i)} 失败: {e}")

# 4. 再次查看 schema
print("\n=== 添加后查看 schema ===")
for yield_clause in ["YIELD schema RETURN schema", "YIELD * RETURN *", ""]:
    try:
        cypher = f"CALL db.getVertexSchema('IntentNode') {yield_clause}".strip()
        r = session.run(cypher)
        for rec in r:
            print(f"  [{yield_clause or 'no-yield'}]: {dict(rec)}")
        break
    except Exception as e:
        print(f"  [{yield_clause or 'no-yield'}] 失败: {e}")

# 5. 直接查看节点属性（包含 embedding2/3/4 是否存在）
print("\n=== 检查新字段 ===")
try:
    r = session.run("MATCH (n:IntentNode {id: 'ccb'}) RETURN keys(n) AS all_keys")
    rec = r.single()
    if rec:
        print(f"  所有属性键: {rec['all_keys']}")
except Exception as e:
    print(f"  失败: {e}")

# 6. 尝试用 Cypher SET 写入 embedding2（string 类型）
print("\n=== 尝试 SET 写入 embedding3 (string) ===")
try:
    r = session.run(
        "MATCH (n:IntentNode {id: 'ccb'}) SET n.embedding3 = 'test_value' RETURN n.embedding3 AS val"
    )
    rec = r.single()
    if rec:
        print(f"  写入成功: {rec['val']}")
    else:
        print("  返回空（可能没匹配到节点）")
except Exception as e:
    print(f"  失败: {e}")

# 7. 尝试用 Cypher SET 写入 embedding2 (FLOAT[])
print("\n=== 尝试 SET 写入 embedding2 (FLOAT[]) ===")
try:
    test_vec = [0.1] * 1024
    r = session.run(
        "MATCH (n:IntentNode {id: 'ccb'}) SET n.embedding2 = $vec RETURN n.embedding2 AS val",
        vec=test_vec,
    )
    rec = r.single()
    if rec and rec['val']:
        print(f"  写入成功！长度: {len(rec['val'])}")
    else:
        print("  返回空")
except Exception as e:
    print(f"  失败: {e}")

# 8. 尝试用 Cypher SET 写入 embedding4 (FLOAT)
print("\n=== 尝试 SET 写入 embedding4 (FLOAT) ===")
try:
    r = session.run(
        "MATCH (n:IntentNode {id: 'ccb'}) SET n.embedding4 = 0.5 RETURN n.embedding4 AS val"
    )
    rec = r.single()
    if rec:
        print(f"  写入成功: {rec['val']}")
    else:
        print("  返回空")
except Exception as e:
    print(f"  失败: {e}")

# 9. 最终检查所有属性键
print("\n=== 最终属性键 ===")
try:
    r = session.run("MATCH (n:IntentNode {id: 'ccb'}) RETURN keys(n) AS all_keys")
    rec = r.single()
    if rec:
        print(f"  所有属性键: {rec['all_keys']}")
except Exception as e:
    print(f"  失败: {e}")

driver.close()
