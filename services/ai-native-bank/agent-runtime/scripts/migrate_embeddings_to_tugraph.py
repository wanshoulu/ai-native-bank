#!/usr/bin/env python
"""一次性迁移脚本：为 TuGraph IntentNode 写入 embedding 向量。

为每个 leaf IntentNode 提取全部可索引文本（title + aliases + description +
recallTerms + recallTermSets + disambiguationHints），调用 Qwen embedding API
生成向量，取平均后写入 IntentNode.embedding 属性。

用法：
    cd services/ai-native-bank/agent-runtime
    python -m scripts.migrate_embeddings_to_tugraph
"""

from __future__ import annotations

import sys
import os
import json
import time
from pathlib import Path

# 确保能 import app.*
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("AI_BANK_TUGRAPH_URI", "bolt://localhost:7687")
os.environ.setdefault("AI_BANK_TUGRAPH_USER", "admin")
os.environ.setdefault("AI_BANK_TUGRAPH_PASSWORD", "73@TuGraph")
os.environ.setdefault("AI_BANK_TUGRAPH_DATABASE", "default")

from neo4j import GraphDatabase
from app.config import RuntimeSettings
from app.tugraph_source import load_v4_payload_from_tugraph
from app.v4_graph_adapter import V4GraphAdapter
from app.semantic_recall_v4 import (
    _load_qwen_key,
    _intent_utterances,
    _EMBEDDING_BATCH_SIZE,
    _MAX_TEXT_CHARS,
)

import httpx

_EMBEDDING_DIMENSIONS = 1024
_EMBEDDING_TIMEOUT = 15.0
_DEFAULT_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
_DEFAULT_MODEL = "text-embedding-v4"


def _embed_batch(client: httpx.Client, texts: list[str], api_key: str,
                  base_url: str, model: str) -> list[list[float]]:
    """调用 Qwen embedding API，批量生成向量"""
    resp = client.post(
        base_url + "/embeddings",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": model,
            "input": texts,
            "dimensions": _EMBEDDING_DIMENSIONS,
        },
    )
    if resp.status_code != 200:
        raise ValueError(f"embedding API status {resp.status_code}: {resp.text[:200]}")
    data = resp.json().get("data", [])
    if len(data) != len(texts):
        raise ValueError(f"embedding count mismatch: {len(data)} != {len(texts)}")
    data.sort(key=lambda item: item.get("index", 0))
    return [item["embedding"] for item in data]


def _average_vectors(vectors: list[list[float]]) -> list[float]:
    """计算多个向量的平均向量"""
    if not vectors:
        return [0.0] * _EMBEDDING_DIMENSIONS
    dim = len(vectors[0])
    result = [0.0] * dim
    for vec in vectors:
        for i, v in enumerate(vec):
            result[i] += v
    n = len(vectors)
    return [v / n for v in result]


def main():
    settings = RuntimeSettings.from_env()
    print(f"TuGraph URI: {settings.tugraph_uri}")

    # 1. 从 TuGraph 加载 V4 图谱
    payload = load_v4_payload_from_tugraph(settings)
    adapter = V4GraphAdapter(payload, Path("."))
    print(f"图谱加载完成: {adapter.graph_id} v{adapter.version}, {len(adapter._intents)} intents")

    # 2. 提取每个 intent 的 utterance 文本
    api_key = _load_qwen_key()
    if not api_key:
        print("错误: QWEN_KEY 未找到")
        sys.exit(1)
    base_url = os.getenv("AI_BANK_MODEL_BASE_URL", _DEFAULT_BASE_URL).rstrip("/")
    model = os.getenv("AI_BANK_EMBEDDING_MODEL", _DEFAULT_MODEL)
    print(f"Embedding API: {base_url}, model: {model}")

    # 为每个 intent 收集 utterance
    intent_utterances: list[tuple[str, str, list[str]]] = []  # (intent_id, v4_node_id, utterances)
    for intent in adapter._intents:
        lifecycle = intent.get("lifecycle", "active")
        if lifecycle in ("deprecated", "retired"):
            continue
        texts = _intent_utterances(intent)
        if not texts:
            continue
        v4_node_id = intent.get("_v4_node_id", "")
        intent_utterances.append((intent["id"], v4_node_id, texts))

    print(f"需要生成 embedding 的 intent 数: {len(intent_utterances)}")

    # 检查缓存
    cache_path = Path(__file__).parent / "v4node_vectors_cache.json"
    if cache_path.exists():
        print(f"发现缓存文件: {cache_path}")
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
        if cached.get("_model") == model and cached.get("_dim") == _EMBEDDING_DIMENSIONS:
            v4node_vectors = cached.get("vectors", {})
            intent_vectors = cached.get("intent_vectors", {})
            print(f"从缓存加载 {len(v4node_vectors)} 个向量，跳过 embedding 生成")
            # 跳转到写入步骤
            _do_write(settings, v4node_vectors)
            return

    # 3. 批量调用 embedding API
    client = httpx.Client(timeout=_EMBEDDING_TIMEOUT)

    # 收集所有 utterance 文本
    all_texts: list[str] = []
    text_mapping: list[tuple[int, int]] = []  # (intent_index, utterance_index)
    for ii, (intent_id, v4_node_id, texts) in enumerate(intent_utterances):
        for ti, text in enumerate(texts):
            all_texts.append(text[:_MAX_TEXT_CHARS])
            text_mapping.append((ii, ti))

    print(f"总 utterance 数: {len(all_texts)}")

    # 分批调用
    all_vectors: list[list[float]] = []
    for i in range(0, len(all_texts), _EMBEDDING_BATCH_SIZE):
        batch = all_texts[i:i + _EMBEDDING_BATCH_SIZE]
        vectors = _embed_batch(client, batch, api_key, base_url, model)
        all_vectors.extend(vectors)
        print(f"  embedding 进度: {len(all_vectors)}/{len(all_texts)}", flush=True)

    client.close()

    # 4. 为每个 intent 计算平均向量
    intent_vectors: dict[str, list[float]] = {}  # intent_id -> avg_vector
    v4node_vectors: dict[str, list[float]] = {}  # v4_node_id -> avg_vector

    utterance_vectors: dict[int, list[list[float]]] = {}
    for k, (ii, ti) in enumerate(text_mapping):
        utterance_vectors.setdefault(ii, []).append(all_vectors[k])

    for ii, (intent_id, v4_node_id, texts) in enumerate(intent_utterances):
        vecs = utterance_vectors.get(ii, [])
        if not vecs:
            continue
        avg = _average_vectors(vecs)
        intent_vectors[intent_id] = avg
        if v4_node_id:
            v4node_vectors[v4_node_id] = avg

    print(f"生成向量数: {len(v4node_vectors)}")

    # 保存缓存
    cache_path.write_text(json.dumps({
        "_model": model,
        "_dim": _EMBEDDING_DIMENSIONS,
        "vectors": v4node_vectors,
        "intent_vectors": intent_vectors,
    }), encoding="utf-8")
    print(f"缓存已保存: {cache_path}")

    # 5. 写入 TuGraph
    _do_write(settings, v4node_vectors)


def _do_write(settings, v4node_vectors: dict[str, list[float]]):
    """将向量写入 TuGraph IntentNode（通过 REST API）。"""
    import requests

    base_url = settings.tugraph_rest_url or "http://localhost:7070"
    db = settings.tugraph_database

    # 登录获取 JWT token
    resp = requests.post(
        f"{base_url}/login",
        json={"user": settings.tugraph_user, "password": settings.tugraph_password},
        headers={"Content-Type": "application/json"},
        timeout=10,
    )
    token = resp.json()["jwt"]
    auth_headers = {"Content-Type": "application/json", "Authorization": f"Bearer {token}"}
    print(f"TuGraph REST API 登录成功")

    def run_cypher(cypher: str, timeout: int = 60):
        r = requests.post(
            f"{base_url}/cypher",
            json={"script": cypher, "graph": db},
            headers=auth_headers,
            timeout=timeout,
        )
        if r.status_code != 200:
            raise Exception(f"HTTP {r.status_code}: {r.text[:400]}")
        return r.json()

    # 5a. 确认 embedding 字段存在
    try:
        result = run_cypher("CALL db.getVertexSchema('IntentNode') YIELD schema RETURN schema")
        rows = result.get("result", [])
        schema = rows[0][0] if rows and rows[0] else {}
        if isinstance(schema, str):
            schema = json.loads(schema)
        props = [p["name"] for p in schema.get("properties", [])]
        if "embedding" not in props:
            print("embedding 字段不存在，尝试添加...")
            run_cypher(
                "CALL db.alterLabelAddFields('vertex', 'IntentNode', "
                "['embedding', 'FLOAT_VECTOR', null, true])"
            )
            print("已添加 embedding 字段 (FLOAT_VECTOR)")
        else:
            print(f"embedding 字段已存在，类型: FLOAT_VECTOR")
    except Exception as e:
        print(f"检查 schema 失败: {e}")

    # 5b. 批量写入 embedding（通过 REST API upsertVertexByJson）
    written = 0
    errors = 0
    for v4_node_id, vec in v4node_vectors.items():
        data = json.dumps([{"id": v4_node_id, "embedding": vec}])
        try:
            result = run_cypher(
                f"CALL db.upsertVertexByJson('IntentNode', '{data}') "
                "YIELD total, data_error, insert, update "
                "RETURN total, data_error, insert, update"
            )
            rows = result.get("result", [])
            if rows and rows[0]:
                _total, _de, _ins, upd = rows[0][0], rows[0][1], rows[0][2], rows[0][3]
                if upd > 0 or _ins > 0:
                    written += 1
                else:
                    errors += 1
                    if errors <= 5:
                        print(f"  警告: 节点 {v4_node_id} data_error={_de}")
            else:
                errors += 1
        except Exception as e:
            errors += 1
            if errors <= 5:
                print(f"  警告: 节点 {v4_node_id} 写入失败: {str(e)[:100]}")
        if written % 50 == 0 and written > 0:
            print(f"  写入进度: {written}/{len(v4node_vectors)}", flush=True)

    print(f"写入完成: {written} 个节点, {errors} 个错误")

    # 5c. 验证写入结果
    try:
        result = run_cypher("MATCH (n:IntentNode) WHERE n.embedding IS NOT NULL RETURN count(n) AS cnt")
        rows = result.get("result", [])
        cnt = rows[0][0] if rows and rows[0] else 0
        print(f"TuGraph 中有 embedding 的节点数: {cnt}")
    except Exception as e:
        print(f"验证写入结果失败: {e}")

    # 5d. 重建向量索引
    try:
        run_cypher("CALL db.deleteVertexVectorIndex('IntentNode', 'embedding')")
        print("已删除旧向量索引")
    except Exception:
        pass

    try:
        run_cypher(
            "CALL db.addVertexVectorIndex('IntentNode', 'embedding', "
            "{dimension: 1024, metric_type: 'cosine', efc: 200, m: 16})"
        )
        print("已创建向量索引: IntentNode.embedding (dim=1024)")
    except Exception as e:
        print(f"创建向量索引失败: {e}")

    # 5e. 验证向量索引
    try:
        result = run_cypher("CALL db.showVertexVectorIndex()")
        rows = result.get("result", [])
        for row in rows:
            print(f"向量索引: label={row[0]}, field={row[1]}, dim={row[3]}, "
                  f"distance={row[4]}, elements={row[6]}")
    except Exception as e:
        print(f"查看向量索引失败: {e}")

    # 5f. 验证 KNN 搜索
    try:
        test_vec = list(v4node_vectors.values())[0]
        vec_literal = "[" + ",".join(str(v) for v in test_vec) + "]"
        result = run_cypher(
            f"CALL db.vertexVectorKnnSearch('IntentNode', 'embedding', {vec_literal}, "
            "{limit: 3}) YIELD node, distance RETURN node.id AS node_id, distance"
        )
        rows = result.get("result", [])
        print(f"KNN 搜索测试: 返回 {len(rows)} 个结果")
        for row in rows:
            print(f"  node_id={row[0]}, distance={row[1]:.4f}")
    except Exception as e:
        print(f"KNN 搜索测试失败: {e}")

    print("迁移完成！")


if __name__ == "__main__":
    main()
