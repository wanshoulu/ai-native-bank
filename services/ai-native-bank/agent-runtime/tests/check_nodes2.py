#!/usr/bin/env python
import json
data = json.load(open(r'd:\project\vscode\data\ai-native-bank\intent-graph\graph.ccb.intents.v2.1.0.json','r',encoding='utf-8'))
tree = data.get('intentTree',[])
targets = ['ccb.channel.credit.apply.apply', 'ccb.channel.credit.apply.overview', 
           'ccb.channel.credit.cash.apply', 'ccb.channel.credit.cash.overview',
           'ccb.channel.credit.card-manage.transactions.query']
for node in tree:
    if node.get('node_role') != 'leaf': continue
    nid = node['id']
    if nid in targets:
        scenes = node.get('scenes',[])
        print(f'=== {nid} ({len(scenes)} scenes) ===')
        for i, scene in enumerate(scenes):
            rp = scene.get('retrieval_profile',{})
            sd = scene.get('semantic_definition',{})
            print(f'  scene[{i}] type={scene.get("scene_type","?")}')
            print(f'    rp.aliases: {rp.get("aliases",[])}')
            print(f'    rp.recallTerms: {rp.get("recallTerms",[])}')
            print(f'    rp.negativeExamples: {rp.get("negativeExamples",[])}')
            print(f'    sd.confusableIntents: {sd.get("confusableIntents",[])}')
        obs = node.get('observability',{})
        records = obs.get('sourceRecords',[])
        for r in records:
            print(f'    sourceRecord: id={r.get("id","")} ref={r.get("ref","")}')
        print()
