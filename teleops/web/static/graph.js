/* TeleOps 节点画布 —— 自研 SVG，零依赖。
   只做三件真正缺的事：平移缩放、拖节点、连线（含自环和回边）；
   节点的属性表单直接复用 app.js 的 schemaForm，弹窗和 toast 也是。 */
(() => {
'use strict';
const T = window.TeleOps;
const { $, $$, esc, GET, PUT, POST, modal, confirmBox, schemaForm, readForm, okToast, errToast, promptToast } = T;
const ANIM = T.anim;

const W = 190, H = 66, PORT_R = 6;
const MIN_K = 0.2, MAX_K = 3;
// 一格鼠标滚轮大约 deltaY=100，乘上这个系数走 exp 曲线约等于 1.16 倍
const WHEEL_SPEED = 0.0015;

let G = null;      // 当前打开的图
let stash = null;  // 未保存的图。页面被重新渲染时用它恢复，别让改动被服务端版本冲掉
let runsTimer = null;
let panelSync = null;  // 当前属性面板的取值函数，面板被拆掉之前必须先调一次

// 面板是用 innerHTML 整块换掉的，而输入框的 change 要等焦点转移才触发：
// 在输入框里打完字直接去点另一个节点，pointerdown 里就把面板拆了，那次 change
// 永远不会来，这次编辑凭空消失——用户看到的就是"改了一个节点必须先保存，
// 否则去改下一个时上一个就丢了"。所以拆之前主动把值收进 G。
function flushPanel() {
  const fn = panelSync;
  panelSync = null;
  if (!fn) return;
  // 面板已经不在了（连续两次重绘）就算了，别让取值把整个交互带崩
  if (!document.getElementById('gp-title')) return;
  try { fn(); } catch (_) { /* 忽略 */ }
}

// ───────────────────────────────────────────────── 几何
const portY = (count, i) => H * (i + 1) / (count + 1);
const outPorts = (n) => (G.types[n.kind] || {}).out_ports || [];
const nodeById = (id) => G.nodes.find((n) => n.node_id === id);

function outPos(n, port) {
  const ps = outPorts(n);
  const i = Math.max(0, ps.findIndex((p) => p.name === port));
  return { x: n.x + W, y: n.y + portY(ps.length || 1, i < 0 ? 0 : i) };
}
const inPos = (n) => ({ x: n.x, y: n.y + H / 2 });

// 这条直线会从哪些节点的框里穿过去。节点画在连线之上，穿过去的那一段就被
// 完全盖住——用户看到的是一张"没有这条线"的图，而引擎照着有这条线的图在跑。
// 「规整」把节点排成中心对齐的一行之后，跨节点的连线必然中招。
function blockers(a, b) {
  if (!G || !G.nodes) return [];
  const lo = Math.min(a.y, b.y) - 6, hi = Math.max(a.y, b.y) + 6;
  const left = Math.min(a.x, b.x) + 8, right = Math.max(a.x, b.x) - 8;
  // 起点和终点自己的框贴着 a/b，靠这 8px 余量排除掉
  return G.nodes.filter((n) => n.x + W > left && n.x < right && n.y < hi && n.y + H > lo);
}

function edgePath(a, b, self) {
  if (self) {
    // 自环：从出口向右绕到节点上方，再从左侧回到入口。规格要求环必须画得出来。
    return `M${a.x},${a.y} C${a.x + 80},${a.y} ${a.x + 80},${a.y - 80} ${a.x - W / 2},${a.y - 80}`
         + ` C${b.x - 80},${b.y - 80} ${b.x - 80},${b.y} ${b.x},${b.y}`;
  }
  if (b.x < a.x + 50) {
    // 回边：绕到下方走，不然会贴着节点画成一团看不清是环
    const dip = Math.max(a.y, b.y) + 110;
    const mid = (a.x + b.x) / 2;
    return `M${a.x},${a.y} C${a.x + 90},${a.y} ${a.x + 90},${dip} ${mid},${dip}`
         + ` C${b.x - 90},${dip} ${b.x - 90},${b.y} ${b.x},${b.y}`;
  }
  const hit = blockers(a, b);
  if (hit.length) {
    // 从下方绕过去。绕出来的弧线本身就是提示：这条连线跳过了中间的节点
    const dip = Math.max(...hit.map((n) => n.y + H)) + 46;
    const mid = (a.x + b.x) / 2;
    return `M${a.x},${a.y} C${a.x + 70},${a.y} ${a.x + 70},${dip} ${mid},${dip}`
         + ` C${b.x - 70},${dip} ${b.x - 70},${b.y} ${b.x},${b.y}`;
  }
  const dx = Math.max(60, Math.abs(b.x - a.x) * 0.45);
  return `M${a.x},${a.y} C${a.x + dx},${a.y} ${b.x - dx},${b.y} ${b.x},${b.y}`;
}

const cut = (s, n = 15) => (s || '').length > n ? (s || '').slice(0, n - 1) + '…' : (s || '');

// ───────────────────────────────────────────────── 渲染
function nodeSvg(n) {
  const t = G.types[n.kind] || {};
  const ps = outPorts(n);
  const sel = G.sel && G.sel.kind === 'node' && G.sel.id === n.node_id;
  const bad = n.status && n.status !== 'ok';
  return `<g class="gnode${sel ? ' sel' : ''}${n.enabled ? '' : ' off'}${bad ? ' bad' : ''}"
      data-node="${esc(n.node_id)}" transform="translate(${n.x},${n.y})">
    <rect class="body" width="${W}" height="${H}" rx="10"></rect>
    <rect class="bar" width="5" height="${H}" rx="2.5" fill="${esc(t.color || '#888')}"></rect>
    <text class="k" x="16" y="24" fill="${esc(t.color || '#888')}">${esc(t.label || n.kind)}</text>
    <text class="t" x="16" y="44">${esc(cut(n.title || n.plugin || '未命名'))}</text>
    <text class="s" x="16" y="59">${esc(cut(subtitle(n), 22))}</text>
    <circle class="port pin" cx="0" cy="${H / 2}" r="${PORT_R}"></circle>
    ${ps.map((p, i) => `<circle class="port pout" data-out="${esc(p.name)}"
        cx="${W}" cy="${portY(ps.length, i)}" r="${PORT_R}"><title>${esc(p.label || '出口')}</title></circle>`).join('')}
    ${ps.length > 1 ? ps.map((p, i) => `<text class="pl" x="${W - 10}" y="${portY(ps.length, i) + 4}">${esc(p.label)}</text>`).join('') : ''}
  </g>`;
}

function subtitle(n) {
  if (n.kind === 'source') {
    const st = n.schedule_type;
    return st === 'cron' ? `cron ${n.schedule_value}`
      : st === 'interval' ? `每 ${n.schedule_value}`
      : st === 'immediate' ? '启用时触发一次' : '手动触发';
  }
  if (n.kind === 'output') {
    const t = (n.config && n.config.targets) || [];
    return `${t.length} 个频道`;
  }
  if (n.kind === 'delay') return `延迟 ${(n.config || {}).value || '?'}`;
  if (n.kind === 'limit') return `最多 ${(n.config || {}).count ?? '?'} 条`;
  if (n.kind === 'merge') return `${(n.config || {}).strategy || 'append'}`;
  if (n.kind === 'router') return (n.config || {}).expr || '未设条件 · 全部走匹配口';
  return n.plugin || '';
}

function edgeSvg(e, i) {
  const a = nodeById(e.src_node), b = nodeById(e.dst_node);
  if (!a || !b) return '';
  const self = e.src_node === e.dst_node;
  const sel = G.sel && G.sel.kind === 'edge' && G.sel.id === i;
  const d = edgePath(outPos(a, e.src_port), inPos(b), self);
  return `<g class="gedge${sel ? ' sel' : ''}${e.enabled ? '' : ' off'}" data-edge="${i}">
    <path class="hit" d="${d}"></path><path class="line" d="${d}" marker-end="url(#arrow)"></path></g>`;
}

function draw() {
  $('#g-edges').innerHTML = G.edges.map(edgeSvg).join('');
  $('#g-nodes').innerHTML = G.nodes.map(nodeSvg).join('');
  applyView();
  paintPanel();
  $('#g-dirty').textContent = G.dirty ? '有未保存的改动' : '';
}

function applyView() {
  const v = G.view;
  $('#g-world').setAttribute('transform', `translate(${v.x},${v.y}) scale(${v.k})`);
  syncZoomUI();
}

const clampK = (k) => Math.min(MAX_K, Math.max(MIN_K, k));

// 以画布内的某个点为锚缩放：那个点在屏幕上的位置保持不动
function zoomAt(k, sx, sy) {
  const next = clampK(k);
  const w = { x: (sx - G.view.x) / G.view.k, y: (sy - G.view.y) / G.view.k };
  G.view.k = next;
  G.view.x = sx - w.x * next;
  G.view.y = sy - w.y * next;
  applyView();
}

// 以画布中心为锚缩放，滑块和 +/- 按钮用
function zoomToCenter(k) {
  const b = $('#gcanvas').getBoundingClientRect();
  zoomAt(k, b.width / 2, b.height / 2);
}

// 滑块走对数刻度：线性刻度下 0.2→1 只占前 30%，手感很别扭
const kToSlider = (k) => Math.round(
  (Math.log(k) - Math.log(MIN_K)) / (Math.log(MAX_K) - Math.log(MIN_K)) * 1000);
const sliderToK = (v) => Math.exp(
  Math.log(MIN_K) + (v / 1000) * (Math.log(MAX_K) - Math.log(MIN_K)));

function syncZoomUI() {
  const slider = $('#g-zoom');
  if (!slider) return;
  slider.value = kToSlider(G.view.k);
  $('#g-zoom-pct').textContent = `${Math.round(G.view.k * 100)}%`;
}

// 改了标题/配置之后只重画这一个节点，别整图重绘——那会把属性面板一起拆掉
function repaintNode(n) {
  const g = $(`[data-node="${CSS.escape(n.node_id)}"]`);
  if (!g) return draw();
  const tmp = document.createElementNS('http://www.w3.org/2000/svg', 'g');
  tmp.innerHTML = nodeSvg(n);
  g.replaceWith(tmp.firstElementChild);
  moveNode(n);
}

// 拖动时只改一个 transform，不整图重绘
function moveNode(n) {
  const g = $(`[data-node="${CSS.escape(n.node_id)}"]`);
  if (g) g.setAttribute('transform', `translate(${n.x},${n.y})`);
  G.edges.forEach((e, i) => {
    if (e.src_node !== n.node_id && e.dst_node !== n.node_id) return;
    const box = $(`[data-edge="${i}"]`);
    if (!box) return;
    const a = nodeById(e.src_node), b = nodeById(e.dst_node);
    const d = edgePath(outPos(a, e.src_port), inPos(b), e.src_node === e.dst_node);
    $$('path', box).forEach((p) => p.setAttribute('d', d));
  });
}

// ───────────────────────────────────────────────── 交互
function toWorld(ev) {
  const r = $('#gcanvas').getBoundingClientRect();
  return { x: (ev.clientX - r.left - G.view.x) / G.view.k, y: (ev.clientY - r.top - G.view.y) / G.view.k };
}

function bindCanvas() {
  const svg = $('#gcanvas');
  let mode = null, start = null, node = null, conn = null;

  svg.onpointerdown = (ev) => {
    if (ev.button !== 0) return;
    const p = toWorld(ev);
    const port = ev.target.closest('.pout');
    const gnode = ev.target.closest('.gnode');
    const gedge = ev.target.closest('.gedge');
    // 某些情况下（合成事件、指针已抬起）会抛，捕获不到也不影响拖动本身
    try { svg.setPointerCapture(ev.pointerId); } catch (_) { /* 忽略 */ }

    if (port && gnode) {
      mode = 'connect';
      conn = { from: gnode.dataset.node, port: port.dataset.out };
      return;
    }
    if (gnode) {
      mode = 'drag';
      node = nodeById(gnode.dataset.node);
      start = { x: p.x - node.x, y: p.y - node.y };
      select('node', node.node_id);
      return;
    }
    if (gedge) { select('edge', +gedge.dataset.edge); return; }
    mode = 'pan';
    start = { x: ev.clientX - G.view.x, y: ev.clientY - G.view.y };
    select(null);
  };

  svg.onpointermove = (ev) => {
    if (!mode) return;
    if (mode === 'pan') {
      G.view.x = ev.clientX - start.x;
      G.view.y = ev.clientY - start.y;
      applyView();
    } else if (mode === 'drag') {
      const p = toWorld(ev);
      node.x = Math.round(p.x - start.x);
      node.y = Math.round(p.y - start.y);
      moveNode(node);
      markDirty();
    } else if (mode === 'connect') {
      const a = outPos(nodeById(conn.from), conn.port);
      const p = toWorld(ev);
      $('#g-preview').setAttribute('d', edgePath(a, p, false));
      $('#g-preview').style.display = '';
    }
  };

  svg.onpointerup = (ev) => {
    if (mode === 'connect') {
      // 不能用 ev.target 找落点：setPointerCapture 之后所有 pointer 事件的 target
      // 都被重定向成 svg 本身（Pointer Events 规范如此），closest('.gnode') 恒为 null。
      // 按世界坐标做矩形命中，既绕开这个坑，也顺带支持"松手在节点任意位置"。
      const p = toWorld(ev);
      const hit = G.nodes.find((n) => p.x >= n.x && p.x <= n.x + W && p.y >= n.y && p.y <= n.y + H);
      if (hit) addEdge(conn.from, conn.port, hit.node_id);
      else errToast('松手时没落在节点上，已取消');
    }
    reset();
  };
  // 系统手势、第二根手指、弹出系统对话框都会发 pointercancel，
  // 不兜住的话 mode 会永远停在 drag，鼠标一动节点就跟着跑
  svg.onpointercancel = reset;
  svg.onlostpointercapture = () => { if (mode === 'connect') reset(); };

  function reset() {
    $('#g-preview').style.display = 'none';
    mode = null; node = null; conn = null;
  }

  svg.onwheel = (ev) => {
    ev.preventDefault();
    // 按实际滚动量缩放，不能每个事件都按固定倍率走：触控板一次两指滑动会发出
    // 几十个小增量事件，固定倍率的话一划就顶到上下限，用起来像是坏的。
    // deltaMode 还要归一化——有的设备按"行"（1）或"页"（2）计，不是像素。
    const unit = ev.deltaMode === 1 ? 16 : ev.deltaMode === 2 ? 400 : 1;
    // macOS 的触控板捏合是带 ctrlKey 的 wheel，手势幅度小，系数给大一点
    const speed = ev.ctrlKey ? WHEEL_SPEED * 6 : WHEEL_SPEED;
    const r = svg.getBoundingClientRect();
    zoomAt(G.view.k * Math.exp(-ev.deltaY * unit * speed),
           ev.clientX - r.left, ev.clientY - r.top);
  };

}

// 只挂一次。bindCanvas 每次进画布页都会跑，挂在里面的话来回切页面就会
// 越堆越多，一次 Delete 触发 N 遍。
document.addEventListener('keydown', onKey);

function onKey(ev) {
  if (!G || !location.hash.endsWith('/graph')) return;
  const tag = (document.activeElement || {}).tagName;
  if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return;
  if ($('#modal-root').firstElementChild) return;      // 弹窗开着时别删背后的节点
  // 只认 Delete。Backspace 在浏览器里是"后退"的肌肉记忆，而焦点经常不在输入框上
  if (ev.key === 'Delete' && G.sel) {
    ev.preventDefault();
    askDelete();
  }
}

function askDelete() {
  const s = G.sel;
  if (!s) return;
  if (s.kind === 'edge') return deleteSelected();     // 连线删了还能再连，不问
  const n = nodeById(s.id);
  const cnt = G.edges.filter((e) => e.src_node === s.id || e.dst_node === s.id).length;
  confirmBox(
    `删除节点「${n ? (n.title || n.plugin || n.node_id) : s.id}」`
    + `${cnt ? `，连同它的 ${cnt} 条连线` : ''}？画布没有撤销，删了要重新配。`,
    deleteSelected);
}

function select(kind, id) {
  flushPanel();          // 先把手上这个节点没提交的编辑收下来，再换面板
  const changed = !G.sel || G.sel.kind !== kind || G.sel.id !== id;
  G.sel = kind ? { kind, id } : null;
  draw();
  // 选中框脉冲一下。只在选中目标真的变了时播，拖动同一个节点不该一直闪
  if (changed && kind === 'node') ANIM.pulse($(`[data-node="${CSS.escape(String(id))}"]`));
}

function markDirty() {
  const was = G.dirty;
  G.dirty = true;
  stash = { id: G.id, nodes: G.nodes, edges: G.edges, view: G.view };
  $('#g-dirty').textContent = '有未保存的改动';
  if (!was) ANIM.flag($('#g-dirty'));   // 从"干净"变"脏"的那一下才提示，之后不再重复
}

function addEdge(src, port, dst) {
  const dup = G.edges.some((e) => e.src_node === src && e.src_port === port && e.dst_node === dst);
  if (dup) return errToast('这两个节点之间已经有同样的连线了');
  const from = nodeById(src);
  if (from.kind === 'output') return errToast('输出是终止节点，不能再往下连');
  G.edges.push({ src_node: src, src_port: port, dst_node: dst, dst_port: 'in', enabled: true });
  markDirty();
  draw();
}

function deleteSelected() {
  const s = G.sel;
  if (!s) return;
  if (s.kind === 'edge') {
    G.edges.splice(s.id, 1);
  } else {
    const n = nodeById(s.id);
    G.nodes = G.nodes.filter((x) => x.node_id !== s.id);
    G.edges = G.edges.filter((e) => e.src_node !== s.id && e.dst_node !== s.id);
    if (n) okToast(`已删除「${n.title || n.plugin || n.node_id}」`);
  }
  G.sel = null;
  markDirty();
  draw();
}

function fit() {
  if (!G.nodes.length) { G.view = { x: 40, y: 40, k: 1 }; return applyView(); }
  const xs = G.nodes.map((n) => n.x), ys = G.nodes.map((n) => n.y);
  const minX = Math.min(...xs) - 40, maxX = Math.max(...xs) + W + 40;
  const minY = Math.min(...ys) - 40, maxY = Math.max(...ys) + H + 40;
  const box = $('#gcanvas').getBoundingClientRect();
  const k = Math.min(MAX_K, Math.max(MIN_K, Math.min(box.width / (maxX - minX), box.height / (maxY - minY))));
  G.view = { k, x: (box.width - (maxX - minX) * k) / 2 - minX * k, y: (box.height - (maxY - minY) * k) / 2 - minY * k };
  applyView();
}

// ───────────────────────────────────────────────── 自动排布
const GAP_X = 90, GAP_Y = 38, ORIGIN_X = 40, ORIGIN_Y = 40;

// 按数据流分层：信息源在最左，每个节点排在所有上游的右边一列。
// 图里允许有环（这是设计的一部分），所以层号要封顶——否则环会把层数推到无穷。
function layerNodes() {
  const depth = new Map();
  const outs = new Map();                     // node_id -> 下游 node_id[]
  const ins = new Map();                      // node_id -> 上游 node_id[]
  G.nodes.forEach((n) => { outs.set(n.node_id, []); ins.set(n.node_id, []); });
  G.edges.forEach((e) => {
    if (!outs.has(e.src_node) || !ins.has(e.dst_node)) return;   // 悬空连线
    if (e.src_node === e.dst_node) return;                       // 自环不参与分层
    outs.get(e.src_node).push(e.dst_node);
    ins.get(e.dst_node).push(e.src_node);
  });

  // 起点：没有入边的节点。整张图是一个环时一条入边都不缺，退而求其次用信息源，
  // 再不行就拿第一个节点当起点——总要有个开头，不然一个节点都排不出来。
  let roots = G.nodes.filter((n) => !ins.get(n.node_id).length).map((n) => n.node_id);
  if (!roots.length) roots = G.nodes.filter((n) => n.kind === 'source').map((n) => n.node_id);
  if (!roots.length && G.nodes.length) roots = [G.nodes[0].node_id];

  const cap = G.nodes.length;                 // 层号上限，环靠它收敛
  const queue = [...roots];
  roots.forEach((id) => depth.set(id, 0));
  while (queue.length) {
    const id = queue.shift();
    const d = depth.get(id);
    for (const nxt of outs.get(id) || []) {
      const cur = depth.get(nxt);
      if (cur !== undefined && cur >= d + 1) continue;   // 已经在更右边了
      if (d + 1 > cap) continue;                          // 环：不再往右推
      depth.set(nxt, d + 1);
      queue.push(nxt);
    }
  }
  // 环里够不着的节点（比如只跟环内节点相连的那些）单独收尾，别把它们丢在原地
  G.nodes.forEach((n) => {
    if (depth.has(n.node_id)) return;
    const up = (ins.get(n.node_id) || []).map((u) => depth.get(u)).filter((v) => v !== undefined);
    depth.set(n.node_id, up.length ? Math.max(...up) + 1 : 0);
  });
  return { depth, ins };
}

function tidy() {
  if (!G.nodes.length) return errToast('画布上还没有节点');
  const { depth, ins } = layerNodes();
  const cols = new Map();                     // 层号 -> 节点[]
  G.nodes.forEach((n) => {
    const d = depth.get(n.node_id) || 0;
    if (!cols.has(d)) cols.set(d, []);
    cols.get(d).push(n);
  });

  // 同层内的上下顺序：按上游节点的平均行号排（重心法），能少掉大部分连线交叉。
  // 没有上游的按当前 y 排，保住用户原来的上下习惯。
  const row = new Map();
  [...cols.keys()].sort((a, b) => a - b).forEach((d) => {
    const list = cols.get(d);
    const key = (n) => {
      const up = (ins.get(n.node_id) || [])
        .map((u) => row.get(u)).filter((v) => v !== undefined);
      return up.length ? up.reduce((a, b) => a + b, 0) / up.length : n.y / (H + GAP_Y);
    };
    list.sort((a, b) => key(a) - key(b));
    list.forEach((n, i) => row.set(n.node_id, i));
  });

  // 每一列在垂直方向居中，列高不齐时不会全靠上挤成阶梯
  const tallest = Math.max(...[...cols.values()].map((l) => l.length));
  const target = new Map();
  cols.forEach((list, d) => {
    const off = (tallest - list.length) / 2;
    list.forEach((n, i) => target.set(n.node_id, {
      x: ORIGIN_X + d * (W + GAP_X),
      y: ORIGIN_Y + (i + off) * (H + GAP_Y),
    }));
  });
  // 让节点滑过去而不是瞬移：瞬移之后没人知道哪个节点去了哪儿，
  // 而这一步的价值恰恰在于让人看清结构被理成了什么样
  markDirty();
  ANIM.tidyNodes(
    G.nodes, target,
    () => G.nodes.forEach(moveNode),          // 每帧重画节点位置和连线
    () => { draw(); fit(); },                  // 落位后再整图重绘、把视野收好
  );
  okToast(`已规整 ${G.nodes.length} 个节点，${cols.size} 层。还没保存，不满意就直接离开`);
}

// ───────────────────────────────────────────────── 属性面板
function paintPanel() {
  const box = $('#gpanel');
  // 先清掉上一次绑的处理器：下面有两个提前 return 的分支，不清的话旧闭包会
  // 继续引用已经被 innerHTML 拆掉的元素
  box.onchange = null;
  box.oninput = null;
  panelSync = null;
  const s = G.sel;
  if (!s) {
    box.innerHTML = `<div class="gp-empty">
      <p>点节点看属性，拖右侧圆点连线。</p>
      <p class="faint">滚轮缩放，空白处拖动平移，选中后按 Delete 删除。</p>
      <p class="faint">允许出现环——消息会重新入队，不是无限递归。</p></div>`;
    return;
  }
  if (s.kind === 'edge') {
    const e = G.edges[s.id];
    if (!e) return;
    box.innerHTML = `<h3>连线</h3>
      <p class="faint">${esc(label(nodeById(e.src_node)))} → ${esc(label(nodeById(e.dst_node)))}</p>
      <div class="field inline"><input type="checkbox" id="ge-on" ${e.enabled ? 'checked' : ''}><label for="ge-on">启用</label></div>
      <button class="danger" id="ge-del">删除连线</button>`;
    $('#ge-on', box).onchange = (ev) => { e.enabled = ev.target.checked; markDirty(); draw(); };
    $('#ge-del', box).onclick = deleteSelected;
    return;
  }
  const n = nodeById(s.id);
  if (!n) return;
  const t = G.types[n.kind] || {};
  const plugins = (t.plugins || []).filter((p) => p.enabled || p.name === n.plugin);
  const schema = t.builtin ? t.config_schema
    : ((plugins.find((p) => p.name === n.plugin) || {}).config_schema || []);

  box.innerHTML = `
    <h3><span class="gp-dot" style="background:${esc(t.color)}"></span>${esc(t.label)}</h3>
    ${t.description ? `<p class="faint">${esc(t.description)}</p>` : ''}
    <div class="field"><label>标题</label><input id="gp-title" value="${esc(n.title)}"></div>
    ${t.builtin ? '' : `<div class="field"><label>插件</label>
      <select id="gp-plugin">${plugins.map((p) => `<option value="${esc(p.name)}" ${p.name === n.plugin ? 'selected' : ''}>${esc(p.display_name)}${p.enabled ? '' : '（已禁用）'}</option>`).join('')}</select></div>`}
    ${n.kind === 'source' ? scheduleHtml(n) : ''}
    <div id="gp-cfg">${schemaForm(schema, n.config || {}, 'gp_')}</div>
    ${n.kind === 'output' ? targetsHtml(n) : ''}
    <div class="field inline"><input type="checkbox" id="gp-on" ${n.enabled ? 'checked' : ''}><label for="gp-on">启用该节点</label></div>
    <button class="danger" id="gp-del">删除节点</button>`;

  const snapshot = () => JSON.stringify(
    [n.title, n.enabled, n.schedule_type, n.schedule_value, n.config]);
  const sync = () => {
    const before = snapshot();
    n.title = $('#gp-title', box).value;
    n.enabled = $('#gp-on', box).checked;
    n.config = { ...(n.config || {}), ...readForm($('#gp-cfg', box)) };
    if (n.kind === 'source') {
      n.schedule_type = $('#gp-sched-type', box).value;
      n.schedule_value = $('#gp-sched-value', box).value;
    }
    if (n.kind === 'output') {
      // 按 channel_id 合并，别整表重建——那会把每个频道的 overrides 和启用状态抹平
      const old = new Map(((n.config || {}).targets || []).map((t) => [t.channel_id, t]));
      n.config.targets = $$('#gp-targets input:checked', box).map((cb) => (
        old.get(+cb.value) || { channel_id: +cb.value, enabled: true, overrides: {} }
      ));
    }
    // 值没变就别标脏：切换选中节点时也会走到这里，无脑标脏会让"只是点了一下
    // 别的节点"也弹出未保存警告，久了用户就不再相信这个提示
    if (snapshot() !== before) markDirty();
  };
  // 只在 change（失焦/勾选）时同步，不用 oninput：每敲一个字就同步没有意义，
  // 而且一旦哪天 sync 里带上重绘，就会变成打一个字掉一次焦点。
  box.onchange = () => { sync(); repaintNode(n); };
  panelSync = sync;
  if (!t.builtin) {
    $('#gp-plugin', box).onchange = (ev) => {
      const next = ev.target.value;
      const prev = n.plugin;
      const apply = () => {
        n._cfgCache = { ...(n._cfgCache || {}), [prev]: n.config || {} };
        n.plugin = next;
        n.config = (n._cfgCache || {})[next] || {};   // 切回去时配置还在
        if (!n.title || n.title === prev) n.title = next;
        markDirty();
        draw();
      };
      // 换插件会把这个节点的配置整套换掉（tg_channel 有十几项），
      // 下拉框获得焦点后误按一下方向键就会触发，必须问一句
      if (Object.keys(n.config || {}).length) {
        confirmBox(`换成「${next}」会把「${prev}」的配置收起来（切回去还在）。继续？`, apply);
        ev.target.value = prev;    // 先还原，确认后由 apply 里的 draw 重画
      } else {
        apply();
      }
    };
  }
  $('#gp-del', box).onclick = askDelete;
}

const label = (n) => n ? (n.title || n.plugin || n.node_id) : '?';

function scheduleHtml(n) {
  const opt = (v, t) => `<option value="${v}" ${n.schedule_type === v ? 'selected' : ''}>${t}</option>`;
  return `<fieldset><legend>什么时候采集</legend>
    <div class="field"><label>触发方式</label><select id="gp-sched-type">
      ${opt('manual', '手动 —— 只能手动运行')}
      ${opt('immediate', '启用时触发一次')}
      ${opt('interval', '固定间隔')}
      ${opt('cron', 'cron 表达式')}
    </select></div>
    <div class="field"><label>间隔 / 表达式</label>
      <input id="gp-sched-value" value="${esc(n.schedule_value || '')}" placeholder="10m 或 0 */2 * * *">
      <div class="help">间隔支持 600、10m、2h、5m-15m（区间随机）；cron 是五段式</div></div>
  </fieldset>`;
}

function targetsHtml(n) {
  const chosen = new Set(((n.config || {}).targets || []).map((t) => t.channel_id));
  return `<fieldset><legend>发布到哪些频道</legend><div id="gp-targets">
    ${G.channels.length ? G.channels.map((c) => `<div class="field inline">
      <input type="checkbox" value="${c.id}" ${chosen.has(c.id) ? 'checked' : ''}>
      <label>${esc(c.title)} <span class="faint">${esc(c.peer)}</span></label></div>`).join('')
    : '<p class="faint">还没有频道，先去频道管理里加。</p>'}
  </div></fieldset>`;
}

// ───────────────────────────────────────────────── 添加节点
// 有些节点"通过了但没有产出"，不写一句话的话用户完全看不出发生了什么
function runNote(r) {
  const d = r.detail || {};
  if ('matched' in d) return d.matched ? '条件命中，走「匹配」口' : '条件未命中，走「其它」口';
  if (d.count) return `合流放行 ${d.count} 条${d.timed_out ? '（等待超时）' : ''}`;
  if (d.fetched !== undefined) return `采集到 ${d.fetched} 条`;
  if (r.status === 'ok' && !r.produced) return '没有下游，到此为止';
  return '';
}

// 最近的逐跳执行记录，画布下面那张表
async function loadRuns() {
  const box = $('#g-runs');
  if (!box) return;
  const d = await GET(`/api/workflows/${G.id}/graph/runs?limit=30`);
  const title = (nid) => {
    const n = nodeById(nid);
    return n ? (n.title || n.plugin || nid) : nid;
  };
  const tag = { ok: '<span class="tag ok">通过</span>', drop: '<span class="tag">终止</span>',
                error: '<span class="tag err">失败</span>', park: '<span class="tag warn">挂起</span>' };
  const waiting = d.queue.filter((m) => m.status !== 'parked');
  const stuck = d.queue.filter((m) => m.status === 'parked');
  // 告警放画布**上方**：画布占满一屏，放在下面的执行记录里用户永远看不到，
  // 而"分支被暂停了"恰恰是最需要立刻知道的事
  const alerts = $('#g-alerts');
  alerts.innerHTML = `
    ${(d.pauses || []).length ? `<div class="gp-alert">
      <strong>有 ${d.pauses.length} 条分支被判定为失控循环，已暂停</strong>
      ${d.pauses.map((p) => `<div class="mt">
        <span class="tag err">${esc(p.reason)}</span> 在「${esc(title(p.node_id))}」上 ·
        ${esc(p.note)} <button class="sm" data-resume="${p.id}">恢复这条分支</button>
        <div class="faint">经过的节点：${esc((p.detail.path_tail || []).join(' → '))}</div>
      </div>`).join('')}
    </div>` : ''}
    ${(d.bad_nodes || []).length ? `<div class="gp-alert">
      <strong>有节点被熔断</strong>
      ${d.bad_nodes.map((n) => `<div class="mt">
        <span class="tag err">${esc(title(n.node_id))}</span> ${esc(n.note)}
        <button class="sm" data-reset-node="${esc(n.node_id)}">恢复这个节点</button></div>`).join('')}
    </div>` : ''}`;

  box.innerHTML = `
    ${waiting.length ? `<p class="faint">队列里还有 ${waiting.length} 条待处理${
      waiting[0].visible_at ? `，最近一条 ${T.fmtTime(waiting[0].visible_at)}` : ''}</p>` : ''}
    ${stuck.length ? `<p class="warn">有 ${stuck.length} 条消息被挂起，可以在下面逐条重投。</p>` : ''}
    ${d.runs.length ? `<div class="t-wrap"><table>
      <thead><tr><th>时间</th><th>节点</th><th>结果</th><th>产出</th><th>说明</th><th></th></tr></thead>
      <tbody>${d.runs.map((r) => `<tr>
        <td class="faint nowrap">${T.fmtTime(r.finished_at || r.started_at)}</td>
        <td>${esc(title(r.node_id))}</td>
        <td>${tag[r.status] || esc(r.status)}</td>
        <td class="mono">${r.produced === 0 && r.status === 'ok' ? '0' : (r.produced || '')}</td>
        <td class="faint">${esc(r.error || runNote(r))}</td>
        <td>${(r.status === 'park' || r.status === 'error') && r.message_id
          ? `<button class="sm" data-requeue="${esc(r.message_id)}"
               data-risky="${r.status === 'error' && (nodeById(r.node_id) || {}).kind === 'output' ? '1' : '0'}"
               >重投</button>` : ''}</td>
      </tr>`).join('')}</tbody></table></div>`
      : '<p class="faint">还没有执行记录。点上面的「触发一次」跑一轮试试。</p>'}`;

  $$('[data-resume]', alerts).forEach((b) => b.onclick = promptToast(async () => {
    const r = await POST(`/api/workflows/${G.id}/graph/pauses/${b.dataset.resume}/resume`);
    okToast(`已恢复，重新入队 ${r.data.requeued} 条`);
    await loadRuns();
  }));
  $$('[data-reset-node]', alerts).forEach((b) => b.onclick = promptToast(async () => {
    await POST(`/api/workflows/${G.id}/graph/nodes/${b.dataset.resetNode}/reset`);
    okToast('已恢复该节点');
    await loadRuns();
    T.render();
  }));
  $$('[data-requeue]', box).forEach((b) => b.onclick = async () => {
    const go = (force) => promptToast(async () => {
      await POST(`/api/workflows/${G.id}/graph/messages/${b.dataset.requeue}/requeue${force ? '?force=1' : ''}`);
      okToast('已重新入队');
      await loadRuns();
    })();
    // 输出节点上失败的消息可能已经发出去一部分，重投会重复发帖
    if (b.dataset.risky === '1') {
      confirmBox('这条消息在输出节点上失败，可能已经发出去一部分。重投会重复发帖，确定继续？',
                 () => go(true));
    } else {
      go(false);
    }
  });
}

function addNodeDialog() {
  const kinds = Object.values(G.types);
  modal({
    title: '添加节点', wide: true,
    body: `<div class="gk-grid">${kinds.map((t) => `
      <div class="gk" data-kind="${esc(t.kind)}">
        <div class="gk-h"><span class="gp-dot" style="background:${esc(t.color)}"></span>${esc(t.label)}</div>
        <div class="faint">${esc(t.description || (t.plugins || []).map((p) => p.display_name).join('、') || '')}</div>
      </div>`).join('')}</div>`,
    onMount: (root, close) => {
      $$('[data-kind]', root).forEach((el) => el.onclick = () => {
        const t = G.types[el.dataset.kind];
        if (!t.builtin && !(t.plugins || []).length) return errToast(`没有可用的${t.label}插件`);
        addNode(el.dataset.kind);
        close();
      });
    },
  });
}

function addNode(kind) {
  const t = G.types[kind];
  const plugin = t.builtin ? '' : (t.plugins[0] || {}).name || '';
  let i = 1;
  while (G.nodes.some((n) => n.node_id === `n${i}`)) i += 1;
  const box = $('#gcanvas').getBoundingClientRect();
  const center = { x: (box.width / 2 - G.view.x) / G.view.k, y: (box.height / 2 - G.view.y) / G.view.k };
  const n = {
    node_id: `n${i}`, kind, plugin, config: {}, title: t.builtin ? t.label : plugin,
    x: Math.round(center.x - W / 2), y: Math.round(center.y - H / 2), enabled: true,
    schedule_type: kind === 'source' ? 'interval' : 'manual',
    schedule_value: kind === 'source' ? '600' : '', jitter: 0, status: 'ok', status_note: '',
  };
  G.nodes.push(n);
  markDirty();
  select('node', n.node_id);
}

// ───────────────────────────────────────────────── 存取
async function save() {
  flushPanel();          // 打完字直接点保存，那次 change 不一定赶得上
  const payload = {
    nodes: G.nodes.map(({ status, status_note, ...n }) => n),
    edges: G.edges,
    viewport: G.view,
  };
  const r = await PUT(`/api/workflows/${G.id}/graph`, payload);
  G.dirty = false;
  stash = null;
  const bad = (r.data || {}).schedule_errors || [];
  // 保存成功但定时任务没建起来，等于这个信息源从此不再自动采集——这条必须显式说
  if (bad.length) errToast(`已保存，但定时没建起来：${bad.join('；')}`);
  else okToast('已保存');
  draw();
}

async function pageGraph(view) {
  G = null;
  const id = +(location.hash.match(/#\/workflows\/(\d+)\/graph/) || [])[1];
  const [d, types] = await Promise.all([
    GET(`/api/workflows/${id}/graph`), GET('/api/graph/node-types'),
  ]);
  // 上次离开时有没保存的改动就接着用，别拿服务端那份把人家的活覆盖掉
  const keep = stash && stash.id === id ? stash : null;
  G = {
    id, types, sel: null, dirty: !!keep,
    name: d.workflow.name, enabled: d.workflow.enabled, channels: d.channels,
    nodes: keep ? keep.nodes : d.nodes,
    edges: keep ? keep.edges : d.edges,
    view: keep ? keep.view : (d.viewport && d.viewport.k ? d.viewport : { x: 40, y: 40, k: 1 }),
  };

  view.innerHTML = `
    <header class="page">
      <div><h1>${esc(G.name)}${G.enabled
        ? '<span class="tag ok" style="margin-left:8px">已启用 · 按信息源的节奏自动运行</span>'
        : '<span class="tag" style="margin-left:8px">已停用 · 只能手动触发</span>'}</h1>
        <p>拖节点排布，从右侧圆点拉一条线到另一个节点。<span id="g-dirty" class="warn"></span></p></div>
      <div class="spacer"></div>
      <div class="btn-row">
        <button id="g-add">+ 添加节点</button>
        <button id="g-fit">适应画布</button>
        <button id="g-trigger">触发一次</button>
        <button class="primary" id="g-save">保存</button>
        <a class="btn" href="#/workflows">返回列表</a>
      </div>
    </header>
    <div id="g-alerts"></div>
    <div class="graph-wrap">
      <div class="gcanvas-box">
      <svg id="gcanvas">
        <defs>
          <marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
            <path d="M0,0 L10,5 L0,10 z"></path>
          </marker>
          <pattern id="grid" width="26" height="26" patternUnits="userSpaceOnUse">
            <circle cx="1" cy="1" r="1"></circle>
          </pattern>
        </defs>
        <rect class="gbg" width="100%" height="100%" fill="url(#grid)"></rect>
        <g id="g-world">
          <g id="g-edges"></g>
          <path id="g-preview" style="display:none"></path>
          <g id="g-nodes"></g>
        </g>
      </svg>
      <div class="gzoom">
        <button class="ghost sm" data-zoom="-1" title="缩小">−</button>
        <input type="range" id="g-zoom" min="0" max="1000" step="1" aria-label="缩放">
        <button class="ghost sm" data-zoom="1" title="放大">+</button>
        <span id="g-zoom-pct" class="mono">100%</span>
        <button class="ghost sm" id="g-zoom-reset" title="回到 100%">1:1</button>
        <span class="gzoom-sep"></span>
        <button class="ghost sm" id="g-tidy" title="按数据流自动排布所有节点">规整</button>
      </div>
      </div>
      <aside id="gpanel"></aside>
    </div>
    <div class="card mt"><h3>执行记录
      <button class="ghost sm" id="g-refresh-runs" style="float:right">刷新</button></h3>
      <div id="g-runs"></div></div>`;

  bindCanvas();
  $('#g-add').onclick = addNodeDialog;
  $('#g-fit').onclick = fit;
  $('#g-zoom').oninput = (ev) => zoomToCenter(sliderToK(+ev.target.value));
  $('#g-zoom-reset').onclick = () => zoomToCenter(1);
  $('#g-tidy').onclick = tidy;
  $$('[data-zoom]').forEach((b) => b.onclick = () =>
    zoomToCenter(G.view.k * (b.dataset.zoom === '1' ? 1.25 : 1 / 1.25)));
  const doTrigger = promptToast(async () => {
    const btn = $('#g-trigger');
    // 请求期间锁住按钮：一次采集要跑几秒到几十秒（真去 Telegram 拉数据 +
    // 输出端的发送间隔），期间没有反馈的话用户会以为没反应而继续点，
    // 每点一次就往队列里再压一批
    btn.disabled = true;
    const old = btn.textContent;
    btn.textContent = '触发中…';
    try {
      const r = await POST(`/api/workflows/${id}/graph/run`);
      okToast(`已触发 ${r.data.sources} 个信息源，去下面的执行记录看`);
      await loadRuns();
    } finally {
      btn.disabled = false;
      btn.textContent = old;
    }
  });
  // 「触发一次」就是跑一次，绝不动启用开关——启用意味着此后每 10 分钟自己跑一轮
  // 往频道发东西，那是完全不同的一件事，不能捎带着做。
  $('#g-trigger').onclick = doTrigger;
  $('#g-save').onclick = promptToast(save);
  $('#g-refresh-runs').onclick = promptToast(loadRuns);
  draw();
  if (G.nodes.length && !(d.viewport && d.viewport.k)) fit();
  loadRuns().catch(() => {});
  // 定期刷新，失控/熔断不用等用户手动点刷新才看得到
  clearInterval(runsTimer);
  runsTimer = setInterval(() => {
    if (!location.hash.endsWith('/graph')) return clearInterval(runsTimer);
    loadRuns().catch(() => {});
  }, 8000);
}

// 关窗、刷新走 beforeunload
window.addEventListener('beforeunload', (ev) => {
  if (G && G.dirty && location.hash.endsWith('/graph')) {
    ev.preventDefault();
    ev.returnValue = '';
  }
});

// 站内换页（点侧边栏、点「返回列表」）不触发 beforeunload，在点击阶段就拦下来。
// 不能等到 hashchange 再拦：那时 app.js 自己的 hashchange 处理器已经跑过一遍
// render()，改回 hash 又会触发第二次，pageGraph 会从服务端重新拉图，
// 未保存的改动当场被冲掉——守卫拦住了跳转却没拦住丢数据。
document.addEventListener('click', (ev) => {
  if (!G || !G.dirty) return;
  const a = ev.target.closest('a[href^="#/"]');
  if (!a || a.getAttribute('href') === location.hash) return;
  ev.preventDefault();
  ev.stopPropagation();
  const to = a.getAttribute('href');
  confirmBox('画布上有未保存的改动，离开就丢了。确定离开？', () => {
    G.dirty = false;
    stash = null;
    location.hash = to;
  });
}, true);

T.registerRoute({
  hash: '#/workflows', hidden: true, page: pageGraph,
  match: (h) => /^#\/workflows\/\d+\/graph$/.test(h),
});
})();
