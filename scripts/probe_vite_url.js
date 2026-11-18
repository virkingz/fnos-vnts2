// 端到端验证：python 的 rewrite_assets 产物 → 经真实 Vite dd() 算法 → 最终 URL。
//
// 通过子进程调用 python 生成改写结果，避免在 JS 里重实现改写逻辑
// （要验证的是 python 真实产物，不是 JS 的模拟）。
const { execFileSync } = require('child_process');
const path = require('path');

const ORIGIN = 'http://127.0.0.1:8080';

// 兼容 python / python3（Linux 常见只有 python3）
function pythonCmd() {
  for (const c of ['python3', 'python']) {
    try { execFileSync(c, ['--version'], { stdio: 'ignore' }); return c; } catch (e) { /* next */ }
  }
  throw new Error('未找到 python 或 python3');
}
const PY = pythonCmd();
const RAW_MAPDEPS = 'const __vite__mapDeps=(i,m=__vite__mapDeps,d=(m.f||(m.f=['
  + '"assets/LoginView-BWeAJnqu.js","assets/modules-BlkSxExa.js",'
  + '"assets/NetworksView-Ce1TIOqS.js","assets/format-BNI7pQiq.js",'
  + '"assets/ConfirmDialog-CkJnAA27.js","assets/ConfirmDialog-BSXJ4v0g.css"]'
  + ')))=>i.map(i=>d[i]);';
const RAW_HTML = '<script type="module" crossorigin src="/assets/index-WuWzUqOK.js"></script>'
  + '<link rel="stylesheet" href="/assets/index-x93Q7xIw.css">';

function rewritten(text, prefix) {
  // 调用 python 的真实实现
  const py = [
    'import sys; sys.path.insert(0, ' + JSON.stringify(path.resolve(__dirname, '..', 'app', 'shell')) + ');',
    'import shell_http as H;',
    'sys.stdout.write(H.rewrite_assets(sys.stdin.read(), ' + JSON.stringify(prefix) + '))',
  ].join(' ');
  return execFileSync(PY, ['-c', py], { input: text, encoding: 'utf-8' });
}

// Vite 运行时算法（取自 v2.0.9 二进制）
const dd = (e) => '/' + e;
const resolve = (t, moduleUrl) => new URL(t, moduleUrl).href;

function checkChunks(prefix, moduleUrl) {
  const out = rewritten(RAW_MAPDEPS, prefix);
  const entries = [...out.matchAll(/"([^"]+\.(?:js|css))"/g)].map(m => m[1]);
  const base = moduleUrl.slice(0, moduleUrl.indexOf('/assets/'));
  let bad = 0;
  for (const e of entries) {
    const final = resolve(dd(e), moduleUrl);
    const want = base + '/' + e.replace(/^.*?(assets\/)/, '$1');
    const ok = final === want && !final.includes('//console') && !final.includes('//app');
    if (!ok) { bad++; console.log('    ✗ %s  →  %s', e, final); }
  }
  return { entries, bad, out };
}

function checkHtml(prefix, moduleUrl) {
  const out = rewritten(RAW_HTML, prefix);
  const refs = [...out.matchAll(/(?:src|href)="([^"]+)"/g)].map(m => m[1]);
  let bad = 0;
  for (const r of refs) {
    const final = new URL(r, moduleUrl).href;   // HTML 资源是绝对路径，不经 dd()
    if (final.includes('//console') || final.includes('//app') ||
        !final.includes(prefix + '/assets/')) {
      bad++; console.log('    ✗ %s → %s', r, final);
    }
  }
  return { refs, bad };
}

const CASES = [
  { name: '网关带前缀 /app/vnts-nas',
    prefix: '/app/vnts-nas/console',
    moduleUrl: ORIGIN + '/app/vnts-nas/console/assets/index-WuWzUqOK.js' },
  { name: '已剥离前缀 /console',
    prefix: '/console',
    moduleUrl: ORIGIN + '/console/assets/index-WuWzUqOK.js' },
];

let totalBad = 0;
for (const c of CASES) {
  console.log('\n=== %s ===', c.name);
  const a = checkChunks(c.prefix, c.moduleUrl);
  console.log('  mapDeps 条目 %d 个，错误 %d 个', a.entries.length, a.bad);
  console.log('  样例 entry : %s', a.entries[0]);
  console.log('  经 dd() 后  : %s', dd(a.entries[0]));
  console.log('  最终 URL   : %s', resolve(dd(a.entries[0]), c.moduleUrl));
  const b = checkHtml(c.prefix, c.moduleUrl);
  console.log('  HTML 引用 %d 个，错误 %d 个', b.refs.length, b.bad);
  console.log('  样例 HTML  : %s', b.refs[0]);
  totalBad += a.bad + b.bad;
}

console.log('\n===== 总错误数：%d =====', totalBad);
process.exit(totalBad === 0 ? 0 : 1);
