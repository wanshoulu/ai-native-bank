"""Load the v4 intent graph payload from TuGraph via Bolt.

Reassembles the payload dict (schemaVersion/graphId/.../intentTree/relations/
resourceRegistries/digest) from the graph imported by import-v4.js, so the
existing V4GraphAdapter can consume it unchanged.
"""

from __future__ import annotations

import json
import logging
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from app.config import RuntimeSettings

logger = logging.getLogger("ai_native_bank.agent_runtime.tugraph")

_NODE_SCALARS = (
    "title",
    "node_role",
    "path",
    "owner",
    "lifecycle",
    "semanticVersion",
    "fulfillmentKind",
    "executionMode",
    "readiness",
    "orchestrationMode",
    "intentType",
    "actionCode",
    "riskLevel",
)
# TuGraph camelCase → V4 payload snake_case
_NODE_SCALAR_RENAME = {
    "fulfillmentKind": "fulfillment_kind",
    "executionMode": "execution_mode",
    "orchestrationMode": "orchestration_mode",
    "intentType": "intent_type",
    "actionCode": "action_code",
    "riskLevel": "risk_level",
}
_NODE_JSON_FIELDS = (
    ("scenesJson", "scenes"),
    ("bindingsJson", "bindings"),
    ("observabilityJson", "observability"),
    ("navigationTermsJson", "navigationTerms"),
    ("scopeInJson", "scope_in"),
)


def _assemble_node(record: Any) -> dict[str, Any]:
    node: dict[str, Any] = {"id": record["id"]}
    for key in _NODE_SCALARS:
        value = record[key]
        if value:
            payload_key = _NODE_SCALAR_RENAME.get(key, key)
            node[payload_key] = value
    # source data carries an explicit parent_intent_id: null on the root node;
    # missing values are stored as '' by the importer
    node["parent_intent_id"] = record["parent_intent_id"] or None
    for stored, original in _NODE_JSON_FIELDS:
        raw = record[stored]
        if raw and raw != "null":
            node[original] = json.loads(raw)
    return node


def load_v4_payload_from_tugraph(settings: "RuntimeSettings") -> dict[str, Any]:
    from neo4j import GraphDatabase

    driver = GraphDatabase.driver(
        settings.tugraph_uri,
        auth=(settings.tugraph_user, settings.tugraph_password),
    )
    try:
        with driver.session(database=settings.tugraph_database) as session:
            meta_record = session.run(
                "MATCH (m:GraphMeta {id: 'meta'}) RETURN m.payloadJson AS payload"
            ).single()
            if meta_record is None:
                raise RuntimeError("GraphMeta not found in TuGraph (empty or foreign graph)")
            meta = json.loads(meta_record["payload"])

            tree = [
                _assemble_node(record)
                for record in session.run(
                    "MATCH (n:IntentNode) RETURN n.id AS id, n.title AS title, "
                    "n.node_role AS node_role, n.parent_intent_id AS parent_intent_id, "
                    "n.path AS path, n.scopeInJson AS scopeInJson, n.owner AS owner, "
                    "n.lifecycle AS lifecycle, n.semanticVersion AS semanticVersion, "
                    "n.seq AS seq, n.scenesJson AS scenesJson, n.bindingsJson AS bindingsJson, "
                    "n.observabilityJson AS observabilityJson, "
                    "n.navigationTermsJson AS navigationTermsJson, "
                    "n.fulfillmentKind AS fulfillmentKind, n.executionMode AS executionMode, "
                    "n.readiness AS readiness, n.orchestrationMode AS orchestrationMode, "
                    "n.intentType AS intentType, n.actionCode AS actionCode, "
                    "n.riskLevel AS riskLevel "
                    "ORDER BY n.seq"
                )
            ]
            # Build node_id → intentType lookup for reconstructing relation IDs.
            # V4.1 relation from/to use format 'node_id#INTENT_TYPE' but TuGraph
            # stores edges between base node IDs (without '#suffix').
            intent_type_map: dict[str, str] = {}
            for node in tree:
                it = node.get("intent_type") or node.get("intentType", "")
                if it:
                    intent_type_map[node["id"]] = it

            def _reconstruct_id(base_id: str) -> str:
                """Reconstruct full relation ID with '#intentType' suffix."""
                it = intent_type_map.get(base_id, "")
                return f"{base_id}#{it}" if it else base_id

            relations = [
                {
                    "id": f"rel.{record['src']}.{record['relType']}.{record['dst']}",
                    "type": record["relType"],
                    "from": _reconstruct_id(record["src"]),
                    "to": _reconstruct_id(record["dst"]),
                    "evidence": record["evidence"],
                }
                for record in session.run(
                    "MATCH (a:IntentNode)-[r:CONFUSABLE_WITH]->(b:IntentNode) "
                    "RETURN a.id AS src, b.id AS dst, r.relType AS relType, "
                    "r.evidence AS evidence, r.seq AS seq ORDER BY r.seq"
                )
            ]
            registries: dict[str, Any] = {}
            for record in session.run(
                "MATCH (n:RegistryNode) RETURN n.id AS id, n.payloadJson AS payload ORDER BY n.id"
            ):
                registries[record["id"].removeprefix("registry.")] = json.loads(record["payload"])
    finally:
        driver.close()

    if not tree:
        raise RuntimeError("TuGraph intent graph is empty (IntentNode count = 0)")

    payload: dict[str, Any] = dict(meta)
    payload["intentTree"] = tree
    payload["relations"] = relations
    payload["resourceRegistries"] = registries
    logger.info(
        "intent graph loaded from TuGraph %s: %d nodes, %d relations, %d registries",
        settings.tugraph_uri,
        len(tree),
        len(relations),
        len(registries),
    )
    return payload
