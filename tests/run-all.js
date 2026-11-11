// 测试启动器：Node 侧测试全跑（Python 侧测试在 agent-runtime/ 内独立运行）
const fs = require('fs');
const path = require('path');
const { spawnSync } = require('child_process');

const dir = __dirname;
const suites = fs.readdirSync(dir).filter(f => f.endsWith('.test.js')).sort();
let totalPass = 0, totalFail = 0, failedSuites = 0;

for (const s of suites) {
  console.log('\n══════ ' + s + ' ══════');
  const r = spawnSync(process.execPath, [path.join(dir, s)], { stdio: 'inherit' });
  // node:test 的退出码即失败数
  if (r.status !== 0) failedSuites++;
}
console.log('\n══════════════════════════════');
console.log('总计: ' + suites.length + ' 套件，' + (failedSuites ? failedSuites + ' 套件有失败' : '全部通过'));
process.exit(failedSuites ? 1 : 0);
