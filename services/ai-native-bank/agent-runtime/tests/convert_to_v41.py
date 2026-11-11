"""V4.0 → V4.1 图谱转换脚本

补全 V4.1 schema 要求的 relations[] 和 resourceRegistries{} 字段，
保持 intentTree 原样（包括 confusableIntents/disambiguationHints/aliases 等字段格式），
兼容现有 V4 Python 引擎（V4GraphAdapter/SubgraphReasoningV4/LLMJudgeV4）。

V4.1 schema 要求（对齐 intentGraphV4.ts）：
  - schemaVersion: "4.1"
  - intentTree: 节点数组（含 scenes/semantic_definition/retrieval_profile/execution_binding）
  - relations: confusable_with/same_as/related_to 边数组（from/to 用 "节点id#场景" 记法）
  - resourceRegistries: {capabilities, faqs, policies, skillBindings}
  - digest: sha256:<hex> of canonicalJson({intentTree, relations, resourceRegistries})

用法：
    python tests/convert_to_v41.py
输出：
    data/ai-native-bank/intent-graph/archive/ccb-mobile-bank-intents.v4.1.json
"""
import json
import hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "data" / "ai-native-bank" / "intent-graph" / "graph.ccb.intents.v2.1.0.json"
DST = ROOT / "data" / "ai-native-bank" / "intent-graph" / "archive" / "ccb-mobile-bank-intents.v4.1.json"


def canonical_json(value):
    """对齐 intentGraphV4.ts 的 canonicalJson：键排序递归 + JSON.stringify（ensure_ascii=False）"""
    if value is None:
        return 'null'
    if isinstance(value, list):
        return '[' + ','.join(canonical_json(v) for v in value) + ']'
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, dict):
        keys = sorted(value.keys())
        return '{' + ','.join(
            json.dumps(k, ensure_ascii=False) + ':' + canonical_json(value[k])
            for k in keys
        ) + '}'
    if isinstance(value, (int, float)):
        # 对齐 JS Number.toString：整数不带小数点，浮点保持原样
        if isinstance(value, float) and value.is_integer():
            return str(int(value))
        return repr(value)
    return json.dumps(value, ensure_ascii=False)


def sha256_hex(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def compute_digest(graph):
    """V4.1 digest: sha256:<hex> of canonicalJson({intentTree, relations, resourceRegistries})"""
    payload = {
        'intentTree': graph['intentTree'],
        'relations': graph['relations'],
        'resourceRegistries': graph['resourceRegistries'],
    }
    return 'sha256:' + sha256_hex(canonical_json(payload))


def find_target_scene_type(target_node, preferred_scene_type):
    """找目标节点的 scene_type：优先同名，否则第一个 scene 的 scene_type"""
    scenes = target_node.get('scenes', []) or []
    if not scenes:
        return None
    for s in scenes:
        st = s.get('scene_type')
        if st == preferred_scene_type:
            return st
    return scenes[0].get('scene_type')


def main():
    data = json.loads(SRC.read_text(encoding='utf-8'))
    tree = data.get('intentTree', [])
    print(f"读入 V4.0: {SRC.name}  ({len(tree)} 节点)")

    # 构建 node_id → node 映射
    nodes_by_id = {}
    for node in tree:
        nid = node.get('id')
        if nid:
            nodes_by_id[nid] = node

    # ── 派生 relations[]：从所有 scene 的 confusableIntents 生成 confusable_with 边 ──
    relations = []
    seen_edges = set()  # 去重：双向只存一条
    missing_targets = set()  # 统计找不到目标的 confusableIntents
    for node in tree:
        src_id = node.get('id')
        if not src_id:
            continue
        for scene in node.get('scenes', []) or []:
            src_scene_type = scene.get('scene_type')
            if not src_scene_type:
                continue
            sd = scene.get('semantic_definition', {}) or {}
            confusable = sd.get('confusableIntents', []) or []
            for target_id in confusable:
                target_node = nodes_by_id.get(target_id)
                if not target_node:
                    missing_targets.add(target_id)
                    continue
                target_scene_type = find_target_scene_type(target_node, src_scene_type)
                if not target_scene_type:
                    missing_targets.add(target_id)
                    continue
                from_ref = f"{src_id}#{src_scene_type}"
                to_ref = f"{target_id}#{target_scene_type}"
                # 去重：双向只存一条（按字典序排）
                edge_key = tuple(sorted([from_ref, to_ref]))
                if edge_key in seen_edges:
                    continue
                seen_edges.add(edge_key)
                relations.append({
                    'id': f"rel.{from_ref}.confusable_with.{to_ref}",
                    'type': 'confusable_with',
                    'from': from_ref,
                    'to': to_ref,
                    'relation_category': 'clarification',
                    'status': 'active',
                    'weight': 1,
                    'evidence': 'derived from scene.semantic_definition.confusableIntents',
                    'condition': '',
                    'owner': '',
                    'version': '',
                })

    print(f"派生 relations[]: {len(relations)} 条 confusable_with 边")
    if missing_targets:
        print(f"  ⚠️ {len(missing_targets)} 个 confusableIntents 目标节点未找到（已跳过）:")
        for mid in sorted(missing_targets)[:10]:
            print(f"    - {mid}")
        if len(missing_targets) > 10:
            print(f"    ... 共 {len(missing_targets)} 个")

    # ── 派生 resourceRegistries{}：从 capabilityBindings 和 answerRefs 提取 ──
    capabilities = set()
    faqs = set()
    for node in tree:
        for scene in node.get('scenes', []) or []:
            tb = scene.get('task_binding', {}) or {}
            slot_contract = tb.get('slotContract', {}) or {}
            for cap in slot_contract.get('capabilityBindings', []) or []:
                if cap:
                    capabilities.add(cap)
            kb = scene.get('knowledge_binding', {}) or {}
            for ref in kb.get('answerRefs', []) or []:
                if ref:
                    faqs.add(ref)

    resource_registries = {
        'capabilities': [{'id': cap} for cap in sorted(capabilities)],
        'faqs': [{'id': faq} for faq in sorted(faqs)],
        'policies': [],
        'skillBindings': [],
    }
    print(f"派生 resourceRegistries: {len(capabilities)} capabilities, {len(faqs)} faqs")

    # ── 构建 V4.1 图谱 ──
    v41_graph = {
        'schemaVersion': '4.1',
        'graphId': data.get('graphId', 'ccb-mobile-bank-intents'),
        'version': data.get('version', '2.1.0'),
        'bank': data.get('bank', 'CCB'),
        'appSurface': data.get('appSurface', 'mock-mobile-bank'),
        'publishedAt': data.get('publishedAt', ''),
        'intentTree': tree,
        'relations': relations,
        'resourceRegistries': resource_registries,
    }

    # 重算 digest
    v41_graph['digest'] = compute_digest(v41_graph)
    print(f"digest: {v41_graph['digest'][:32]}...")

    # 输出
    DST.write_text(json.dumps(v41_graph, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f"\n输出 V4.1: {DST}")
    print(f"  schemaVersion: 4.0 → 4.1")
    print(f"  节点数: {len(v41_graph['intentTree'])}")
    print(f"  relations: {len(relations)} 条 confusable_with 边")
    print(f"  resourceRegistries: cap={len(capabilities)}, faq={len(faqs)}")


if __name__ == '__main__':
    main()
