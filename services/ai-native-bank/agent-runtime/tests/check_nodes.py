#!/usr/bin/env python
import json
data = json.load(open(r'd:\project\vscode\data\ai-native-bank\intent-graph\graph.ccb.intents.v2.1.0.json','r',encoding='utf-8'))
tree = data.get('intentTree',[])
for node in tree:
    if node.get('node_role') != 'leaf': continue
    nid = node['id']
    if 'transactions' in nid or 'cash.apply' in nid or 'apply.apply' in nid:
        scenes = node.get('scenes',[])
        sd = scenes[0].get('semantic_definition',{}) if scenes else {}
        obs = node.get('observability',{})
        records = obs.get('sourceRecords',[])
        v2_ids = [r.get('id') for r in records if 'v2-source' in r.get('id','')]
        aliases = sd.get('aliases',[])
        recallTerms = sd.get('recallTerms',[])
        confusables = sd.get('confusableIntents',[])
        print(f'V4 ID: {nid}')
        print(f'  aliases: {aliases}')
        print(f'  recallTerms: {recallTerms}')
        print(f'  confusableIntents: {confusables}')
        print(f'  V2 IDs: {v2_ids}')
        print()
