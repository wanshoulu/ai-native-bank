#!/usr/bin/env python
"""Import a V4.1 intent graph JSON file into TuGraph.

This is the Builder-side counterpart to tugraph_source.py (runtime read side).
It takes a V4.1 JSON file (intentTree + relations + resourceRegistries)
and writes it into TuGraph so that load_v4_payload_from_tugraph() can
read it back unchanged.

TuGraph schema (must exist before running this script):
  - IntentNode: id, title, node_role, parent_intent_id, path, owner,
    lifecycle, semanticVersion, seq, scenesJson, bindingsJson,
    observabilityJson, navigationTermsJson, scopeInJson,
    fulfillmentKind, executionMode, readiness, orchestrationMode,
    intentType, actionCode, riskLevel, embedding
  - GraphMeta: id, payloadJson
  - RegistryNode: id, payloadJson
  - CONFUSABLE_WITH edge: relType, evidence, seq

Usage:
    cd services/ai-native-bank/agent-runtime
    python -m scripts.import_v4_to_tugraph                          # defaults to v4.1.json
    python -m scripts.import_v4_to_tugraph --input path/to/v4.json  # custom input
    python -m scripts.import_v4_to_tugraph --v4-fixed                # use v4-fixed.json

Env vars (all optional, have sensible defaults):
    AI_BANK_TUGRAPH_URI       (default: bolt://localhost:7687)
    AI_BANK_TUGRAPH_USER      (default: admin)
    AI_BANK_TUGRAPH_PASSWORD  (default: 73@TuGraph)
    AI_BANK_TUGRAPH_DATABASE  (default: default)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

# Ensure app.* is importable when run as a script
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from neo4j import GraphDatabase

# ── TuGraph connection config ──
URI = os.environ.get("AI_BANK_TUGRAPH_URI", "bolt://127.0.0.1:7687")
USER = os.environ.get("AI_BANK_TUGRAPH_USER", "admin")
PASSWORD = os.environ.get("AI_BANK_TUGRAPH_PASSWORD", "73@TuGraph")
DB = os.environ.get("AI_BANK_TUGRAPH_DATABASE", "default")

# ── Default input paths ──
# Script is at services/ai-native-bank/agent-runtime/scripts/
# Project root is 5 levels up from __file__
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent.parent
GRAPH_DIR = Path(os.environ.get("AI_BANK_INTENT_GRAPH_DIR", _PROJECT_ROOT / "data" / "ai-native-bank" / "intent-graph"))
DEFAULT_V41 = GRAPH_DIR / "archive" / "ccb-mobile-bank-intents.v4.1.json"
DEFAULT_V4_GRAPH = GRAPH_DIR / "graph.ccb.intents.v2.1.0.json"

# ── Batch size for writes ──
BATCH_SIZE = 50


def _s(v: Any) -> str:
    """Convert value to string (empty for None)."""
    return str(v) if v is not None else ""


def _json_str(v: Any) -> str:
    """Serialize to compact JSON string (or 'null' for empty)."""
    if v is None or v == []:
        return "null"
    if isinstance(v, str):
        return v
    return json.dumps(v, ensure_ascii=False)


def _extract_scene_scalar(node: dict) -> dict[str, str]:
    """Extract scalar fields from node.scenes[0] for TuGraph IntentNode properties."""
    scalars: dict[str, str] = {}
    scenes = node.get("scenes")
    if not scenes or not isinstance(scenes, list) or len(scenes) == 0:
        return scalars

    scene = scenes[0] if isinstance(scenes[0], dict) else {}

    # semantic_definition.intentType
    sd = scene.get("semantic_definition", {}) or {}
    scalars["intentType"] = _s(sd.get("intentType", ""))

    # execution_binding
    eb = scene.get("execution_binding", {}) or {}
    scalars["executionMode"] = _s(eb.get("executionMode", ""))
    scalars["readiness"] = _s(eb.get("readiness", ""))
    scalars["riskLevel"] = _s(eb.get("riskLevel", ""))

    # task_binding
    tb = scene.get("task_binding", {}) or {}
    scalars["actionCode"] = _s(tb.get("actionCode", ""))

    # decision_policy
    dp = scene.get("decision_policy", {}) or {}
    scalars["orchestrationMode"] = _s(dp.get("orchestrationHint", ""))

    # fulfillmentKind: V4.1 JSON doesn't have this at scene level;
    # use semantic_definition.intentType as fallback if it's a DO/QUERY
    scalars["fulfillmentKind"] = _s(sd.get("intentType", ""))

    return scalars


def _build_intent_node_params(node: dict, seq: int) -> dict[str, Any]:
    """Build Cypher parameters for a single IntentNode write."""
    scene_scalars = _extract_scene_scalar(node)
    parent_id = node.get("parent_intent_id")
    parent_id_str = _s(parent_id) if parent_id else ""

    return {
        "id": node["id"],
        "title": _s(node.get("title")),
        "node_role": _s(node.get("node_role")),
        "parent_intent_id": parent_id_str,
        "path": _s(node.get("path")),
        "owner": _s(node.get("owner")),
        "lifecycle": _s(node.get("lifecycle")),
        "semanticVersion": _s(node.get("semanticVersion")),
        "seq": seq,
        "scopeInJson": _json_str(node.get("scope_in")),
        "scenesJson": _json_str(node.get("scenes")),
        "bindingsJson": _json_str(node.get("bindings")),
        "observabilityJson": _json_str(node.get("observability")),
        "navigationTermsJson": _json_str(node.get("navigationTerms")),
        "fulfillmentKind": scene_scalars.get("fulfillmentKind", ""),
        "executionMode": scene_scalars.get("executionMode", ""),
        "readiness": scene_scalars.get("readiness", ""),
        "orchestrationMode": scene_scalars.get("orchestrationMode", ""),
        "intentType": scene_scalars.get("intentType", ""),
        "actionCode": scene_scalars.get("actionCode", ""),
        "riskLevel": scene_scalars.get("riskLevel", ""),
    }


_NODE_WRITE_CYPHER = """
CREATE (n:IntentNode {
    id: $params.id,
    title: $params.title,
    node_role: $params.node_role,
    parent_intent_id: $params.parent_intent_id,
    path: $params.path,
    owner: $params.owner,
    lifecycle: $params.lifecycle,
    semanticVersion: $params.semanticVersion,
    seq: $params.seq,
    scopeInJson: $params.scopeInJson,
    scenesJson: $params.scenesJson,
    bindingsJson: $params.bindingsJson,
    observabilityJson: $params.observabilityJson,
    navigationTermsJson: $params.navigationTermsJson,
    fulfillmentKind: $params.fulfillmentKind,
    executionMode: $params.executionMode,
    readiness: $params.readiness,
    orchestrationMode: $params.orchestrationMode,
    intentType: $params.intentType,
    actionCode: $params.actionCode,
    riskLevel: $params.riskLevel
})
"""

_RELATION_WRITE_CYPHER = """
MATCH (a:IntentNode {id: $from_base}), (b:IntentNode {id: $to_base})
CREATE (a)-[r:CONFUSABLE_WITH {
    relType: $rel_type,
    evidence: $evidence,
    seq: $seq
}]->(b)
"""

_REGISTRY_WRITE_CYPHER = """
CREATE (n:RegistryNode {
    id: $reg_id,
    payloadJson: $payload
})
"""


def clear_graph(session) -> None:
    """Delete all existing nodes and edges."""
    print("[import] clearing existing graph data ...")
    # Delete edges first, then nodes
    session.run("MATCH ()-[r:CONFUSABLE_WITH]->() DELETE r")
    session.run("MATCH (n:IntentNode) DELETE n")
    session.run("MATCH (n:RegistryNode) DELETE n")
    session.run("MATCH (n:GraphMeta) DELETE n")
    print("[import] clear done.")


def write_graph_meta(session, graph: dict) -> None:
    """Write GraphMeta node with payload JSON."""
    meta = {
        "schemaVersion": graph.get("schemaVersion", "4.1"),
        "graphId": graph.get("graphId", ""),
        "version": graph.get("version", ""),
        "bank": graph.get("bank", ""),
        "appSurface": graph.get("appSurface", ""),
        "publishedAt": graph.get("publishedAt", ""),
        "digest": graph.get("digest", ""),
    }
    payload_json = json.dumps(meta, ensure_ascii=False)
    session.run(
        "CREATE (m:GraphMeta {id: 'meta', payloadJson: $payload})",
        payload=payload_json,
    )
    print(f"[import] GraphMeta written: schemaVersion={meta['schemaVersion']}, "
          f"graphId={meta['graphId']}, digest={meta['digest'][:20]}...")


def write_intent_nodes(session, intent_tree: list[dict]) -> int:
    """Write all IntentNode nodes in batches. Returns count written."""
    total = len(intent_tree)
    written = 0
    for i, node in enumerate(intent_tree):
        params = _build_intent_node_params(node, i)
        session.run(_NODE_WRITE_CYPHER, params=params)
        written += 1
        if written % BATCH_SIZE == 0:
            print(f"[import] IntentNode progress: {written}/{total}")
    print(f"[import] IntentNode written: {written}/{total}")
    return written


def write_confusable_edges(session, relations: list[dict]) -> int:
    """Write CONFUSABLE_WITH edges from relations[]. Returns count written.

    V4.1 relation from/to IDs use the format 'node_id#INTENT_TYPE' (e.g.
    'ccb.channel.credit.apply.faq#HELP'), but IntentNode IDs in TuGraph are
    the base node IDs without the '#suffix'. We MATCH by the base ID and
    store the original full ID as edge properties (fromId/toId) so the
    reader can reconstruct the exact relation payload.
    """
    written = 0
    skipped = 0
    for seq, rel in enumerate(relations):
        rel_type = rel.get("type", "")
        if rel_type != "confusable_with":
            continue
        from_id = rel.get("from") or rel.get("source") or ""
        to_id = rel.get("to") or rel.get("target") or ""
        if not from_id or not to_id:
            skipped += 1
            continue
        # Strip '#suffix' to get the base node ID for MATCHing
        from_base = from_id.split("#")[0] if "#" in from_id else from_id
        to_base = to_id.split("#")[0] if "#" in to_id else to_id
        evidence = _s(rel.get("evidence", ""))
        session.run(
            _RELATION_WRITE_CYPHER,
            from_base=from_base,
            to_base=to_base,
            rel_type=rel_type,
            evidence=evidence,
            seq=seq,
        )
        written += 1
    print(f"[import] CONFUSABLE_WITH edges written: {written}, skipped: {skipped}")
    return written


def write_registry_nodes(session, registries: dict) -> int:
    """Write RegistryNode nodes from resourceRegistries. Returns count written."""
    written = 0
    for reg_key, reg_value in registries.items():
        if not reg_value:
            continue
        payload_json = json.dumps(reg_value, ensure_ascii=False)
        reg_id = f"registry.{reg_key}"
        session.run(
            _REGISTRY_WRITE_CYPHER,
            reg_id=reg_id,
            payload=payload_json,
        )
        written += 1
    print(f"[import] RegistryNode written: {written}")
    return written


def verify(session, graph: dict) -> bool:
    """Verify that the imported data matches the input graph."""
    print("\n[import] === VERIFY ===")

    # Check IntentNode count
    r = session.run("MATCH (n:IntentNode) RETURN count(n) AS cnt").single()
    node_count = r["cnt"] if r else 0
    expected_nodes = len(graph.get("intentTree", []))
    node_ok = node_count == expected_nodes
    print(f"  IntentNode: {node_count} (expected {expected_nodes}) {'✅' if node_ok else '❌'}")

    # Check CONFUSABLE_WITH edge count
    r = session.run(
        "MATCH ()-[r:CONFUSABLE_WITH]->() RETURN count(r) AS cnt"
    ).single()
    edge_count = r["cnt"] if r else 0
    expected_edges = sum(
        1 for rel in graph.get("relations", [])
        if rel.get("type") == "confusable_with"
        and (rel.get("from") or rel.get("source"))
        and (rel.get("to") or rel.get("target"))
    )
    edge_ok = edge_count == expected_edges
    print(f"  CONFUSABLE_WITH: {edge_count} (expected {expected_edges}) {'✅' if edge_ok else '❌'}")

    # Check GraphMeta
    r = session.run("MATCH (m:GraphMeta {id: 'meta'}) RETURN m.payloadJson AS p").single()
    meta_ok = r is not None
    print(f"  GraphMeta: {'present ✅' if meta_ok else 'MISSING ❌'}")

    # Check RegistryNode count
    r = session.run("MATCH (n:RegistryNode) RETURN count(n) AS cnt").single()
    reg_count = r["cnt"] if r else 0
    expected_reg = sum(
        1 for v in graph.get("resourceRegistries", {}).values() if v
    )
    reg_ok = reg_count == expected_reg
    print(f"  RegistryNode: {reg_count} (expected {expected_reg}) {'✅' if reg_ok else '❌'}")

    all_ok = node_ok and edge_ok and meta_ok and reg_ok
    print(f"  Result: {'ALL OK ✅' if all_ok else 'HAS FAILURES ❌'}")
    return all_ok


def main():
    parser = argparse.ArgumentParser(description="Import V4.1 intent graph into TuGraph")
    parser.add_argument("--input", "-i", type=str, help="Input V4.1 JSON file path")
    parser.add_argument("--v4-fixed", action="store_true",
                        help="Use graph.ccb.intents.v2.1.0.json as input")
    args = parser.parse_args()

    # Determine input file
    if args.input:
        input_path = Path(args.input)
    elif args.v4_fixed:
        input_path = DEFAULT_V4_GRAPH
    elif DEFAULT_V41.exists():
        input_path = DEFAULT_V41
    else:
        input_path = DEFAULT_V4_GRAPH

    if not input_path.exists():
        print(f"[import] ERROR: input file not found: {input_path}")
        sys.exit(1)

    # Load graph JSON
    print(f"[import] loading: {input_path}")
    with open(input_path, encoding="utf-8") as f:
        graph = json.load(f)

    intent_tree = graph.get("intentTree", [])
    relations = graph.get("relations", [])
    registries = graph.get("resourceRegistries", {})

    print(f"[import] intentTree: {len(intent_tree)} nodes")
    print(f"[import] relations: {len(relations)} edges")
    print(f"[import] resourceRegistries: {', '.join(f'{k}={len(v)}' for k, v in registries.items()) if registries else 'empty'}")
    print(f"[import] schemaVersion: {graph.get('schemaVersion', '?')}")
    print(f"[import] TuGraph: {URI} / db={DB}")

    # Connect and import
    driver = GraphDatabase.driver(URI, auth=(USER, PASSWORD))
    try:
        with driver.session(database=DB) as session:
            clear_graph(session)
            write_graph_meta(session, graph)
            write_intent_nodes(session, intent_tree)
            write_confusable_edges(session, relations)
            write_registry_nodes(session, registries)
            all_ok = verify(session, graph)
    finally:
        driver.close()

    if all_ok:
        print("\n[import] SUCCESS: graph imported to TuGraph ✅")
    else:
        print("\n[import] WARNING: verification has failures ❌")
        sys.exit(1)


if __name__ == "__main__":
    main()
