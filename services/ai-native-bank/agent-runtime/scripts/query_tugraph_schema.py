#!/usr/bin/env python
"""查询 TuGraph 过程签名并尝试添加 embedding 字段。"""
import os
from neo4j import GraphDatabase

URI = "bolt://localhost:7687"
USER = "admin"
PASSWORD = "73@TuGraph"
DB = "default"

driver = GraphDatabase.driver(URI, auth=(USER, PASSWORD))
session = driver.session(database=DB)

# 1. 查询 db.alterLabelAddFields 签名
print("=== 查询 db.alterLabelAddFields 签名 ===")
try:
    r = session.run(
        "CALL dbms.procedures() YIELD name, signature, description "
        "WHERE name = 'db.alterLabelAddFields' "
        "RETURN name, signature, description"
    )
    for record in r:
        print(f"name: {record['name']}")
        print(f"signature: {record['signature']}")
        print(f"description: {record['description']}")
except Exception as e:
    print(f"查询失败: {e}")

# 2. 查看当前 IntentNode schema
print("\n=== 当前 IntentNode schema ===")
try:
    r = session.run("CALL db.getVertexSchema('IntentNode') YIELD * RETURN *")
    for record in r:
        print(record)
except Exception as e:
    print(f"查询schema失败: {e}")

# 3. 尝试添加 embedding 字段
print("\n=== 尝试添加 embedding 字段 ===")
try:
    # 尝试格式1: (label, fields_list)
    r = session.run(
        "CALL db.alterLabelAddFields("
        "  'IntentNode', "
        "  [{name: 'embedding', type: 'FLOAT', optional: true, dimension: 1024}]"
        ")"
    )
    print("添加成功 (格式1)")
except Exception as e:
    print(f"格式1失败: {e}")
    # 尝试格式2: 三个参数 (label, fields, default_values)
    try:
        r = session.run(
            "CALL db.alterLabelAddFields("
            "  'IntentNode', "
            "  [{name: 'embedding', type: 'FLOAT', optional: true, dimension: 1024}], "
            "  [{name: 'embedding', value: null}]"
            ")"
        )
        print("添加成功 (格式2)")
    except Exception as e2:
        print(f"格式2失败: {e2}")
        # 尝试格式3: 类型用 string
        try:
            r = session.run(
                "CALL db.alterLabelAddFields("
                "  'IntentNode', "
                "  [{name: 'embedding', type: 'float', optional: true, dimension: 1024}]"
                ")"
            )
            print("添加成功 (格式3)")
        except Exception as e3:
            print(f"格式3失败: {e3}")

# 4. 再次查看 schema
print("\n=== 添加后的 IntentNode schema ===")
try:
    r = session.run("CALL db.getVertexSchema('IntentNode') YIELD * RETURN *")
    for record in r:
        print(record)
except Exception as e:
    print(f"查询schema失败: {e}")

driver.close()
