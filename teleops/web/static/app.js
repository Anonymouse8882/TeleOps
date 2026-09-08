/* TeleOps 后台前端 —— 无框架单页应用 */
(() => {
'use strict';

// ─────────────────────────────────────────────────────────── 工具
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

const TOKEN_KEY = 'teleops_token';
const token = () => localStorage.getItem(TOKEN_KEY) || '';

async function api(path, options = {}) {
  const opt = { headers: { 'X-Token': token() }, ...options };
  if (opt.body && !(opt.body instanceof FormData)) {
    opt.headers['Content-Type'] = 'application/json';
    opt.body = JSON.stringify(opt.body);
  }
  const res = await fetch(path, opt);
  let data = {};
  try { data = await res.json(); } catch (_) { /* 空响应 */ }
  if (!res.ok) {
    if (res.status === 401) { promptToken(); }
    throw new Error(data.detail || data.error || `HTTP ${res.status}`);
  }
  return data.data !== undefined ? data : { data: null, ...data };
}
const GET = (p) => api(p).then((r) => r.data);
const POST = (p, body) => api(p, { method: 'POST', body });
const PATCH = (p, body) => api(p, { method: 'PATCH', body });
const PUT = (p, body) => api(p, { method: 'PUT', body });
const DEL = (p) => api(p, { method: 'DELETE' });

// 动效层。anim.js 没加载出来时给一组空实现，页面照常能用，只是没有动画
const ANIM = window.TeleOpsAnim || {
  enabled: () => false, pageEnter() {}, countUp() {}, toastIn() {},
  toastOut: (el, done) => done && done(), modalIn() {},
  modalOut: (el, done) => done && done(),
  tidyNodes: (n, t, f, d) => { n.forEach((x) => { const v = t.get(x.node_id); if (v) { x.x = v.x; x.y = v.y; } }); f && f(); d && d(); },
  pulse() {}, flag() {},
};

function toast(msg, kind = '') {
  const el = document.createElement('div');
  el.className = 'toast ' + kind;
  el.innerHTML = esc(msg);
  $('#toasts').appendChild(el);
  ANIM.toastIn(el);
  setTimeout(() => ANIM.toastOut(el, () => el.remove()), kind === 'err' ? 5200 : 2800);
}
const okToast = (m) => toast(m, 'ok');
const errToast = (m) => toast(m, 'err');

function promptToast(fn) {
  return async (...args) => {
    try { await fn(...args); } catch (e) { errToast(e.message); }
  };
}

function promptToken() {
  const t = prompt('该后台已开启口令保护，请输入访问口令：', token());
  if (t !== null) { localStorage.setItem(TOKEN_KEY, t.trim()); location.reload(); }
}

const fmtTime = (iso) => {
  if (!iso) return '—';
  const d = new Date(iso.endsWith('Z') || iso.includes('+') ? iso : iso + 'Z');
  if (isNaN(d)) return '—';
  const diff = (Date.now() - d.getTime()) / 1000;
  // 秒级要看得见：采集间隔可以短到几秒，全都取整成"1 分钟后"的话，改了间隔的人
  // 看到的永远是同一行字，只会得出"改了不生效"的结论
  if (diff >= 0 && diff < 10) return '刚刚';
  if (diff >= 0 && diff < 60) return `${Math.floor(diff)} 秒前`;
  if (diff >= 0 && diff < 3600) return `${Math.floor(diff / 60)} 分钟前`;
  if (diff < 0 && diff > -60) return `${Math.ceil(-diff)} 秒后`;
  if (diff < 0 && diff > -3600) return `${Math.ceil(-diff / 60)} 分钟后`;
  return d.toLocaleString('zh-CN', { hour12: false, month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' });
};
const fmtDuration = (s) => {
  if (!s) return '0 秒';
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
  return d ? `${d} 天 ${h} 小时` : h ? `${h} 小时 ${m} 分` : `${m || 1} 分钟`;
};
const STATUS_TAG = {
  ok: '<span class="tag ok">成功</span>', partial: '<span class="tag warn">部分成功</span>',
  error: '<span class="tag err">失败</span>', running: '<span class="tag info">运行中</span>',
  skipped: '<span class="tag">跳过</span>', never: '<span class="tag">未运行</span>',
};

// ─────────────────────────────────────────────────────────── 全局缓存
const store = { plugins: [], channels: [], accounts: [], workflows: [], settings: null };

async function refreshStore() {
  const [plugins, channels, accounts, settings] = await Promise.all([
    api('/api/plugins'), GET('/api/channels'), GET('/api/accounts'), GET('/api/settings'),
  ]);
  store.plugins = plugins.data;
  store.pluginStats = plugins.stats;
  store.pluginErrors = plugins.errors || [];
  store.channels = channels;
  store.accounts = accounts;
  store.settings = settings;
}

const tgApi = () => (store.settings && store.settings.telegram) || { configured: false, api_id: 0 };

// api 没配好时，账号相关的操作都做不了——给一条能点过去的提示
const apiWarning = () => tgApi().configured ? '' : `
  <div class="card" style="border-color:var(--warn);margin-bottom:14px">
    <strong>还没有配置 Telegram API</strong>
    <p class="faint" style="margin:6px 0 10px">
      所有账号共用一套 api_id / api_hash，需要先在设置里填好才能登录或导入账号。</p>
    <button class="primary sm" data-goto-settings>去设置</button>
  </div>`;

function bindApiWarning(root) {
  $$('[data-goto-settings]', root).forEach((b) => b.onclick = () => { location.hash = '#/settings'; });
}
const pluginsOf = (type) => store.plugins.filter((p) => p.type === type);
const pluginByName = (name) => store.plugins.find((p) => p.name === name);
const channelById = (id) => store.channels.find((c) => c.id === id);

// ─────────────────────────────────────────────────────────── 模态框
// 弹窗是一个栈：新开的压在旧的上面，关闭只撤掉自己那一层，底下正在编辑的
// 表单原样留着（例如在工作流编辑里点「测试信息源」）。
// dismissible 只给只读弹窗用——填表的弹窗点遮罩、按 Esc 都不关，免得填到
// 一半被误关丢内容。
const modalStack = [];
const topModal = () => modalStack[modalStack.length - 1] || null;

const FOCUSABLE = 'input:not([type=hidden]), select, textarea, button, a[href], [tabindex]:not([tabindex="-1"])';
const focusables = (el) => $$(FOCUSABLE, el).filter((n) => !n.disabled && n.offsetParent !== null);

function modal({ title, body, footer, wide = false, dismissible = false, onMount }) {
  const opener = document.activeElement;
  const layer = document.createElement('div');
  layer.className = 'modal-layer';
  layer.innerHTML = `
    <div class="mask">
      <div class="modal ${wide ? 'wide' : ''}" role="dialog" aria-modal="true" tabindex="-1">
        <div class="modal-head"><h2>${esc(title)}</h2><button class="ghost sm" data-close>✕</button></div>
        <div class="modal-body">${body}</div>
        <div class="modal-foot">${footer || '<button data-close>关闭</button>'}</div>
      </div>
    </div>`;
  $('#modal-root').appendChild(layer);
  ANIM.modalIn(layer);

  let closed = false;
  const close = () => {
    if (closed) return;
    closed = true;
    const i = modalStack.indexOf(entry);
    if (i >= 0) modalStack.splice(i, 1);
    // 焦点立刻还回去，元素等退场动画播完再摘——反过来的话按 Esc 连关两层时，
    // 焦点会落在一个正在消失的元素上
    ANIM.modalOut(layer, () => layer.remove());
    const below = topModal();
    if (below) below.focus();                                     // 焦点还给下面那层
    else if (opener && document.contains(opener)) opener.focus();  // 没有了就还给打开它的按钮
  };
  const entry = {
    layer, dismissible, close,
    focus: () => {
      const first = focusables($('.modal-body', layer))
        .find((n) => ['INPUT', 'SELECT', 'TEXTAREA'].includes(n.tagName));
      (first || $('.modal', layer)).focus();
    },
  };
  modalStack.push(entry);

  $$('[data-close]', layer).forEach((b) => b.onclick = close);

  // 点遮罩关闭要求「按下和抬起都落在遮罩上」——不然从弹窗里拖选文字、
  // 松手时落到遮罩上也会被当成点了外面。
  const mask = $('.mask', layer);
  let downOnMask = false;
  mask.onmousedown = (e) => { downOnMask = e.target === e.currentTarget; };
  mask.onclick = (e) => {
    const outside = downOnMask && e.target === e.currentTarget;
    downOnMask = false;
    if (outside && dismissible) close();
  };

  if (onMount) onMount(layer, close);
  entry.focus();
  return close;
}
const closeModal = () => { const t = topModal(); if (t) t.close(); };

// Esc 关掉最上层的只读弹窗；Tab 锁在最上层弹窗内，不会跑到后面的页面上。
document.addEventListener('keydown', (e) => {
  const top = topModal();
  if (!top) return;
  if (e.key === 'Escape') {
    if (top.dismissible && !e.isComposing && e.keyCode !== 229) { e.preventDefault(); top.close(); }
    return;
  }
  if (e.key !== 'Tab') return;
  const items = focusables(top.layer);
  if (!items.length) return;
  const first = items[0], last = items[items.length - 1];
  const cur = document.activeElement;
  if (!top.layer.contains(cur)) { e.preventDefault(); (e.shiftKey ? last : first).focus(); }
  else if (e.shiftKey && cur === first) { e.preventDefault(); last.focus(); }
  else if (!e.shiftKey && cur === last) { e.preventDefault(); first.focus(); }
});

function confirmBox(text, onYes) {
  modal({
    title: '请确认', body: `<p>${esc(text)}</p>`, dismissible: true,
    footer: '<button data-close>取消</button><button class="primary danger" data-yes>确定</button>',
    onMount: (root, close) => { $('[data-yes]', root).onclick = async () => { close(); await onYes(); }; },
  });
}

// ─────────────────────────────────────────────────────────── 动态表单
function fieldHtml(f, value, idPrefix = '') {
  const v = value === undefined || value === null ? (f.default ?? '') : value;
  const id = `${idPrefix}f_${f.name}`;
  const label = `${esc(f.label)}${f.required ? ' <span class="req">*</span>' : ''}`;
  const help = f.help ? `<div class="help">${esc(f.help)}</div>` : '';
  const ph = f.placeholder ? ` placeholder="${esc(f.placeholder)}"` : '';
  let input;
  switch (f.type) {
    case 'bool':
      return `<div class="field inline"><input type="checkbox" id="${id}" data-f="${f.name}" data-t="bool" ${v ? 'checked' : ''}>
              <label for="${id}">${label}</label></div>${help}`;
    case 'text':
      input = `<textarea id="${id}" data-f="${f.name}" data-t="text"${ph}>${esc(v)}</textarea>`; break;
    case 'int': case 'float':
      input = `<input type="number" id="${id}" data-f="${f.name}" data-t="${f.type}" step="${f.type === 'int' ? 1 : 'any'}" value="${esc(v)}"${ph}>`; break;
    case 'password':
      input = `<input type="password" id="${id}" data-f="${f.name}" data-t="string" value="${esc(v)}"${ph}>`; break;
    case 'select':
      input = `<select id="${id}" data-f="${f.name}" data-t="string">${(f.options || []).map((o) =>
        `<option value="${esc(o.value)}" ${String(o.value) === String(v) ? 'selected' : ''}>${esc(o.label)}</option>`).join('')}</select>`; break;
    case 'multiselect': {
      const arr = Array.isArray(v) ? v.map(String) : [];
      input = `<select multiple id="${id}" data-f="${f.name}" data-t="multi">${(f.options || []).map((o) =>
        `<option value="${esc(o.value)}" ${arr.includes(String(o.value)) ? 'selected' : ''}>${esc(o.label)}</option>`).join('')}</select>`; break;
    }
    case 'json':
      input = `<textarea id="${id}" data-f="${f.name}" data-t="json">${esc(typeof v === 'string' ? v : JSON.stringify(v ?? {}, null, 2))}</textarea>`; break;
    case 'channel':
      input = `<select id="${id}" data-f="${f.name}" data-t="string"><option value="">— 不指定 —</option>${store.channels.map((c) =>
        `<option value="${esc(c.peer)}" ${c.peer === v ? 'selected' : ''}>${esc(c.title)} (${esc(c.peer)})</option>`).join('')}</select>`; break;
    case 'account':
      input = `<select id="${id}" data-f="${f.name}" data-t="int"><option value="">— 用工作流账号 —</option>${store.accounts.map((a) =>
        `<option value="${a.id}" ${String(a.id) === String(v) ? 'selected' : ''}>${esc(a.name)}</option>`).join('')}</select>`; break;
    default:
      input = `<input type="text" id="${id}" data-f="${f.name}" data-t="string" value="${esc(v)}"${ph}>`;
  }
  return `<div class="field"><label for="${id}">${label}</label>${input}${help}</div>`;
}

function schemaForm(schema, values = {}, idPrefix = '') {
  if (!schema || !schema.length) return '<p class="faint">该插件无需配置。</p>';
  const groups = new Map();
  schema.forEach((f) => {
    const g = f.group || '';
    if (!groups.has(g)) groups.set(g, []);
    groups.get(g).push(f);
  });
  let html = '';
  for (const [g, fields] of groups) {
    const inner = `<div class="form-grid">${fields.map((f) => fieldHtml(f, values[f.name], idPrefix)).join('')}</div>`;
    html += g ? `<fieldset><legend>${esc(g)}</legend>${inner}</fieldset>` : inner;
  }
  return html;
}

function readForm(root) {
  const out = {};
  $$('[data-f]', root).forEach((el) => {
    const name = el.dataset.f, t = el.dataset.t;
    let v;
    if (t === 'bool') v = el.checked;
    else if (t === 'int') v = el.value === '' ? null : parseInt(el.value, 10);
    else if (t === 'float') v = el.value === '' ? null : parseFloat(el.value);
    else if (t === 'multi') v = [...el.selectedOptions].map((o) => o.value);
    else if (t === 'json') { try { v = el.value.trim() ? JSON.parse(el.value) : {}; } catch (_) { v = {}; } }
    else v = el.value;
    out[name] = v;
  });
  return out;
}

// ─────────────────────────────────────────────────────────── 页面：概览
async function pageOverview(view) {
  const d = await GET('/api/overview');
  const c = d.counters;
  view.innerHTML = `
    <header class="page">
      <div><h1>概览</h1><p>运行 ${fmtDuration(d.uptime)} · 插件 ${d.plugins.source + d.plugins.filter + d.plugins.formatter + d.plugins.sink} 个</p></div>
      <div class="spacer"></div>
      <button id="refresh">刷新</button>
    </header>
    <div class="grid c4">
      ${statCard('近 24 小时发布', c.sent_24h, `${c.runs_24h} 次运行 · ${c.failed_24h} 次失败`)}
      ${statCard('工作流', `${c.workflows_enabled}/${c.workflows}`, '已启用 / 总数')}
      ${statCard('频道', `${c.channels_enabled}/${c.channels}`, '已启用 / 总数')}
      ${statCard('账号', `${c.accounts_online}/${c.accounts}`, '在线 / 总数')}
    </div>

    <div class="grid c2 mt">
      <div class="card">
        <h3>工作流状态</h3>
        <div class="t-wrap"><table>
          <thead><tr><th>名称</th><th>状态</th><th>上次</th><th>下次</th><th class="right">累计发布</th></tr></thead>
          <tbody>${d.workflow_status.length ? d.workflow_status.map((w) => `
            <tr>
              <td><a data-nav="#/workflows">${esc(w.name)}</a> ${w.enabled ? '' : '<span class="tag">停用</span>'}</td>
              <td>${STATUS_TAG[w.last_status] || esc(w.last_status)}</td>
              <td class="faint nowrap">${fmtTime(w.last_run_at)}</td>
              <td class="faint nowrap">${w.enabled ? fmtTime(w.next_run_at) : '—'}</td>
              <td class="right">${w.total_sent}</td>
            </tr>`).join('') : '<tr><td colspan="5" class="faint">还没有工作流</td></tr>'}
          </tbody>
        </table></div>
      </div>
      <div class="card">
        <h3>最近运行</h3>
        <div class="t-wrap"><table>
          <thead><tr><th>工作流</th><th>结果</th><th>采集/发布</th><th>时间</th></tr></thead>
          <tbody>${d.recent_runs.length ? d.recent_runs.map((r) => `
            <tr title="${esc(r.error || '')}">
              <td>${esc(r.workflow)} <span class="tag">${r.trigger === 'manual' ? '手动' : r.trigger === 'test' ? '试运行' : '定时'}</span></td>
              <td>${STATUS_TAG[r.status] || esc(r.status)}</td>
              <td class="mono">${r.fetched} / ${r.sent}${r.failed ? ` <span class="tag err">${r.failed}</span>` : ''}</td>
              <td class="faint nowrap">${fmtTime(r.started_at)}</td>
            </tr>`).join('') : '<tr><td colspan="4" class="faint">暂无运行记录</td></tr>'}
          </tbody>
        </table></div>
      </div>
    </div>

    <div class="grid c2 mt">
      <div class="card">
        <h3>调度队列</h3>
        ${d.jobs.length ? `<div class="t-wrap"><table><tbody>${d.jobs.map((j) => `
          <tr><td>${esc(j.name)}</td><td class="faint mono">${esc(j.trigger)}</td><td class="right faint nowrap">${fmtTime(j.next_run)}</td></tr>`).join('')}
        </tbody></table></div>` : '<p class="faint">没有已排期的任务。启用工作流后会出现在这里。</p>'}
      </div>
      <div class="card">
        <h3>插件</h3>
        <div class="chain">
          <span class="node">信息源 ${d.plugins.source}</span><span class="arrow">→</span>
          <span class="node">过滤 ${d.plugins.filter}</span><span class="arrow">→</span>
          <span class="node">格式化 ${d.plugins.formatter}</span><span class="arrow">→</span>
          <span class="node">输出 ${d.plugins.sink}</span>
        </div>
        <p class="faint mt">去重记录 ${c.dedup_records} 条 · 历史累计发布 ${c.sent_total} 条
        ${d.plugins.error ? `<br><span class="tag err">${d.plugins.error} 个插件加载失败</span>` : ''}</p>
      </div>
    </div>`;
  $('#refresh').onclick = () => render();
  $$('[data-nav]', view).forEach((a) => a.onclick = () => location.hash = a.dataset.nav);
  // 数字滚动放在最后：每 20 秒的自动刷新里，只有真的变了的那个数字会动，
  // 其余的一动不动——动起来的那一格就是"这 20 秒里发生的事"
  $$('.stat .value[data-count]', view).forEach((el) =>
    ANIM.countUp(el, +el.dataset.v, el.dataset.count));
}

// 纯数字的值打上标记，渲染完由 countUp 滚动到位（只有数值变了才滚）
const statCard = (label, value, sub) =>
  `<div class="card stat"><div class="value"${Number.isFinite(+value) && String(value).trim() !== ''
    ? ` data-count="${esc(label)}" data-v="${esc(value)}"` : ''}>${esc(value)}</div>` +
  `<div class="label">${esc(label)}</div><div class="sub faint">${esc(sub || '')}</div></div>`;

// ─────────────────────────────────────────────────────────── 页面：频道
async function pageChannels(view) {
  const list = store.channels;
  view.innerHTML = `
    <header class="page">
      <div><h1>频道管理</h1><p>被运营的频道/群组。一个频道可以挂载多个工作流。</p></div>
      <div class="spacer"></div>
      <div class="btn-row">
        <button id="import">从账号导入</button>
        <button class="primary" id="add">+ 添加频道</button>
      </div>
    </header>
    <div class="card">
      <div class="t-wrap"><table>
        <thead><tr><th>标题</th><th>标识</th><th>账号</th><th>类型</th><th>挂载工作流</th><th>状态</th><th></th></tr></thead>
        <tbody>${list.length ? list.map((c) => `
          <tr>
            <td>${esc(c.title)}${c.note ? `<br><span class="faint">${esc(c.note)}</span>` : ''}</td>
            <td class="mono">${esc(c.peer)}</td>
            <td>${esc(accountName(c.account_id))}</td>
            <td><span class="tag">${c.kind === 'group' ? '群组' : '频道'}</span></td>
            <td>${c.workflow_count ? `<a data-wf="${c.id}">${c.workflow_count} 个</a>` : '<span class="faint">未挂载</span>'}</td>
            <td>${c.enabled ? '<span class="tag ok">启用</span>' : '<span class="tag">停用</span>'}
                ${c.resolved && c.resolved.id ? `<span class="tag info" title="成员 ${c.resolved.participants ?? '?'}">已校验</span>` : ''}</td>
            <td class="actions">
              <button class="sm" data-check="${c.id}">校验</button>
              <button class="sm" data-edit="${c.id}">编辑</button>
              <button class="sm danger" data-del="${c.id}">删除</button>
            </td>
          </tr>`).join('') : `<tr><td colspan="7"><div class="empty"><div class="big">📢</div>还没有频道。先在「账号」里登录，再从账号导入或手动添加。</div></td></tr>`}
        </tbody>
      </table></div>
    </div>`;

  $('#add').onclick = () => channelForm(null);
  $('#import').onclick = importDialog;
  $$('[data-edit]', view).forEach((b) => b.onclick = () => channelForm(channelById(+b.dataset.edit)));
  $$('[data-del]', view).forEach((b) => b.onclick = () => {
    const c = channelById(+b.dataset.del);
    confirmBox(`删除频道「${c.title}」？挂载在它上面的工作流目标会一并解除。`, promptToast(async () => {
      await DEL(`/api/channels/${c.id}`); okToast('已删除'); await refreshStore(); render();
    }));
  });
  $$('[data-check]', view).forEach((b) => b.onclick = promptToast(async () => {
    b.disabled = true;
    try {
      const r = await POST(`/api/channels/${b.dataset.check}/resolve`);
      okToast(`可达：${r.data.title || r.data.username || r.data.id}`);
      await refreshStore(); render();
    } finally { b.disabled = false; }
  }));
  $$('[data-wf]', view).forEach((a) => a.onclick = promptToast(async () => {
    const d = await GET(`/api/channels/${a.dataset.wf}`);
    modal({
      title: `「${d.title}」上的工作流`, dismissible: true,
      body: d.workflows.length ? `<div class="t-wrap"><table>
        <thead><tr><th>工作流</th><th>信息源</th><th>状态</th><th>上次运行</th></tr></thead>
        <tbody>${d.workflows.map((w) => `<tr><td>${esc(w.name)}</td><td class="mono">${esc(w.source_plugin)}</td>
          <td>${w.enabled ? '<span class="tag ok">启用</span>' : '<span class="tag">停用</span>'}</td>
          <td class="faint">${fmtTime(w.last_run_at)}</td></tr>`).join('')}</tbody></table></div>`
        : '<p class="faint">暂无</p>',
    });
  }));
}

const accountName = (id) => (store.accounts.find((a) => a.id === id) || {}).name || '未绑定';

function channelForm(ch) {
  const c = ch || { title: '', peer: '', account_id: null, kind: 'channel', note: '', enabled: true };
  modal({
    title: ch ? '编辑频道' : '添加频道',
    body: `
      <div class="form-grid">
        <div class="field"><label>显示名称 <span class="req">*</span></label><input data-f="title" data-t="string" value="${esc(c.title)}"></div>
        <div class="field"><label>频道标识 <span class="req">*</span></label>
          <input data-f="peer" data-t="string" value="${esc(c.peer)}" placeholder="@channel 或 -1001234567890">
          <div class="help">支持 @用户名、数字 ID 或 t.me 链接</div></div>
        <div class="field"><label>使用账号</label><select data-f="account_id" data-t="int">
          <option value="">— 未绑定 —</option>
          ${store.accounts.map((a) => `<option value="${a.id}" ${a.id === c.account_id ? 'selected' : ''}>${esc(a.name)}</option>`).join('')}
        </select></div>
        <div class="field"><label>类型</label><select data-f="kind" data-t="string">
          <option value="channel" ${c.kind === 'channel' ? 'selected' : ''}>频道</option>
          <option value="group" ${c.kind === 'group' ? 'selected' : ''}>群组</option>
        </select></div>
      </div>
      <div class="field"><label>备注</label><textarea data-f="note" data-t="string" style="min-height:56px">${esc(c.note)}</textarea></div>
      <div class="field inline"><input type="checkbox" data-f="enabled" data-t="bool" ${c.enabled ? 'checked' : ''}><label>启用</label></div>`,
    footer: '<button data-close>取消</button><button class="primary" data-save>保存</button>',
    onMount: (root, close) => {
      $('[data-save]', root).onclick = promptToast(async () => {
        const v = readForm(root);
        if (!v.title || !v.peer) return errToast('名称和标识都要填');
        if (ch) await PATCH(`/api/channels/${ch.id}`, v); else await POST('/api/channels', v);
        close(); okToast('已保存'); await refreshStore(); render();
      });
    },
  });
}

function importDialog() {
  if (!store.accounts.length) return errToast('请先添加并登录一个账号');
  modal({
    title: '从账号导入频道', wide: true,
    body: `<div class="field"><label>选择账号</label><select id="imp-acc">
        ${store.accounts.map((a) => `<option value="${a.id}">${esc(a.name)}${a.status === 'online' ? '' : '（未登录）'}</option>`).join('')}
      </select></div>
      <button class="primary" id="imp-load">读取对话列表</button>
      <div id="imp-list" class="mt"></div>`,
    footer: '<button data-close>关闭</button><button class="primary" id="imp-save" disabled>导入所选</button>',
    onMount: (root, close) => {
      $('#imp-load', root).onclick = promptToast(async () => {
        const id = $('#imp-acc', root).value;
        $('#imp-list', root).innerHTML = '<p class="faint">读取中…</p>';
        const rows = await GET(`/api/accounts/${id}/dialogs`);
        const exist = new Set(store.channels.map((c) => c.peer));
        $('#imp-list', root).innerHTML = `<div class="t-wrap"><table>
          <thead><tr><th style="width:32px"></th><th>名称</th><th>标识</th><th>类型</th><th>成员</th></tr></thead>
          <tbody>${rows.map((r, i) => `<tr>
            <td><input type="checkbox" data-peer="${i}" ${exist.has(r.peer) ? 'disabled' : ''} style="width:auto"></td>
            <td>${esc(r.title)} ${r.admin ? '<span class="tag info">管理员</span>' : ''}</td>
            <td class="mono">${esc(r.peer)}${exist.has(r.peer) ? ' <span class="tag">已存在</span>' : ''}</td>
            <td><span class="tag">${r.kind === 'group' ? '群组' : '频道'}</span></td>
            <td class="faint">${r.participants ?? '—'}</td></tr>`).join('')}</tbody></table></div>`;
        root._rows = rows;
        $('#imp-save', root).disabled = false;
      });
      $('#imp-save', root).onclick = promptToast(async () => {
        const picked = $$('[data-peer]:checked', root).map((cb) => root._rows[+cb.dataset.peer]);
        if (!picked.length) return errToast('没有勾选任何频道');
        const r = await POST('/api/channels/bulk', { account_id: +$('#imp-acc', root).value, peers: picked });
        close(); okToast(`已导入 ${r.data.created} 个频道`); await refreshStore(); render();
      });
    },
  });
}

// ─────────────────────────────────────────────────────────── 页面：工作流
async function pageWorkflows(view) {
  const list = await GET('/api/workflows');
  store.workflows = list;
  view.innerHTML = `
    <header class="page">
      <div><h1>工作流</h1><p>信息源 → 过滤 → 格式化 → 输出。每个工作流可同时发布到多个频道。</p></div>
      <div class="spacer"></div>
      <button class="primary" id="add">+ 新建工作流</button>
    </header>
    ${list.length ? `<div class="grid c2">${list.map(wfCard).join('')}</div>`
      : `<div class="card"><div class="empty"><div class="big">⚙️</div>还没有工作流。<br>新建一个，把 TG 频道搬运源挂到你的频道上试试。</div></div>`}`;

  $('#add').onclick = newWorkflowDialog;
  $$('[data-toggle]', view).forEach((b) => b.onclick = promptToast(async () => {
    const r = await POST(`/api/workflows/${b.dataset.toggle}/toggle`);
    okToast(r.data.enabled
      ? '已启用，此后会按信息源的节奏自动运行'
      : `已停用${r.data.parked ? `，挂起了 ${r.data.parked} 条在途消息` : ''}`);
    render();
  }));
  // 「立即运行」只跑这一次，不改启用状态
  $$('[data-run]', view).forEach((b) => b.onclick = () => runWorkflow(+b.dataset.run, false, b));
  $$('[data-dry]', view).forEach((b) => b.onclick = () => runWorkflow(+b.dataset.dry, true, b));
  $$('[data-runs]', view).forEach((b) => b.onclick = () => showRuns(+b.dataset.runs));
  $$('[data-del]', view).forEach((b) => b.onclick = () => {
    const wf = list.find((w) => w.id === +b.dataset.del);
    confirmBox(`删除工作流「${wf.name}」？运行记录和游标会一并清除。`, promptToast(async () => {
      await DEL(`/api/workflows/${wf.id}`); okToast('已删除'); render();
    }));
  });
  $$('[data-reset]', view).forEach((b) => b.onclick = () => {
    const id = +b.dataset.reset;
    confirmBox('重置该工作流的采集游标和去重记录？重置后可能会重复发布已发过的内容。', promptToast(async () => {
      const r = await POST(`/api/workflows/${id}/reset?mode=all`);
      const d = r.data || {};
      // 如实说清楚清掉了什么。之前不管有没有清到东西都弹「已重置」，
      // 而图模式下它其实一行都没动。
      okToast(d.cursor || d.dedup
        ? `已重置：游标等状态 ${d.cursor} 项、去重记录 ${d.dedup} 条`
        : '没有可重置的记录（本来就是空的）');
      render();
    }));
  });
}

function wfCard(w) {
  const chain = [
    pluginLabel(w.source_plugin),
    ...(w.filters || []).filter((s) => s.enabled !== false).map((s) => pluginLabel(s.plugin)),
    ...(w.formatters || []).filter((s) => s.enabled !== false).map((s) => pluginLabel(s.plugin)),
    pluginLabel(w.sink_plugin),
  ];
  // 调度在画布的信息源节点上，一条工作流可以有好几个各跑各的。w.schedule_value
  // 是线性时代的字段，图模式下没人执行它——照它显示的话，改了节点间隔的人会看到
  // 一个永远不变的"每 600 秒"。只有还没建图的工作流才回落到那个老字段。
  const srcs = w.sources || [];
  const sched = srcs.length
    ? srcs.map((s) => `${esc(s.title)} ${esc(s.schedule_label)}${s.enabled ? '' : '（已停用）'}`).join(' · ')
    : (w.schedule_type === 'manual' ? '仅手动触发'
      : w.schedule_type === 'cron' ? `cron ${esc(w.schedule_value)}`
      : `每 ${esc(w.schedule_value)}${/^\d+$/.test(String(w.schedule_value)) ? ' 秒' : ''} 循环`);
  // "下次"取最早到期的那个源；有多个源时要说是哪一个，否则这个时间没法对上任何一行
  const nextSrc = srcs.filter((s) => s.enabled && s.next_run_at)
    .sort((a, b) => (a.next_run_at < b.next_run_at ? -1 : 1))[0];
  const nextAt = nextSrc ? nextSrc.next_run_at : w.next_run_at;
  const showNext = w.enabled && (srcs.length ? !!nextSrc : w.schedule_type !== 'manual');
  const limit = w.daily_limit ? ` · 每天上限 ${w.daily_limit} 条` : '';
  return `<div class="card">
    <div style="display:flex;align-items:center;gap:9px;margin-bottom:8px">
      <strong style="font-size:15px">${esc(w.name)}</strong>
      ${w.enabled ? '<span class="tag ok">持续运行中</span>' : '<span class="tag">已停用</span>'}
      ${w.dry_run ? '<span class="tag warn">试运行模式</span>' : ''}
      <span class="spacer" style="flex:1"></span>
      ${STATUS_TAG[w.last_status] || ''}
    </div>
    ${w.description ? `<p class="faint" style="margin:0 0 9px">${esc(w.description)}</p>` : ''}
    <div class="chain">${chain.map((c, i) => `${i ? '<span class="arrow">→</span>' : ''}<span class="node">${esc(c)}</span>`).join('')}</div>
    <p class="faint mt" style="margin-bottom:10px;font-size:12px">
      📤 ${w.targets.length ? w.targets.map((t) => esc(t.title)).join('、') : '<span class="tag err">未选频道</span>'}
      <br>⏱ ${sched} · 每轮 ${w.max_items_per_run} 条${limit}
      <br>🕘 ${esc(w.window_label || '全天')} · ${esc(w.days_label || '每天')}
      <br>📈 已运行 ${w.total_runs || 0} 轮 · 累计发布 ${w.total_sent} 条
      <br>上次 ${fmtTime(w.last_run_at)}${showNext
        ? ` · <strong>下次 ${fmtTime(nextAt)}</strong>${
          nextSrc && srcs.length > 1 ? `（${esc(nextSrc.title)}）` : ''}` : ''}
      ${w.last_note ? `<br><span class="tag warn">${esc(w.last_note)}</span>` : ''}
    </p>
    <div class="btn-row">
      <button class="sm primary" data-run="${w.id}">立即运行</button>
      <button class="sm" data-dry="${w.id}">试运行</button>
      <button class="sm" data-toggle="${w.id}">${w.enabled ? '停用' : '启用'}</button>
      <a class="btn sm" href="#/workflows/${w.id}/graph">打开画布</a>
      <button class="sm" data-runs="${w.id}">记录</button>
      <button class="sm" data-reset="${w.id}">重置游标</button>
      <button class="sm danger" data-del="${w.id}">删除</button>
    </div>
  </div>`;
}

const pluginLabel = (name) => (pluginByName(name) || {}).display_name || name || '?';

async function runWorkflow(id, dry, btn) {
  const old = btn.textContent;
  btn.disabled = true; btn.textContent = dry ? '试运行中…' : '运行中…';
  try {
    const r = await POST(`/api/workflows/${id}/run`, { dry_run: dry });
    const d = r.data;
    // 图模式的"立即运行"是投一条触发消息就返回，结果去画布的执行记录里看；
    // 试运行则是同步遍历，照旧直接出预览
    if (d.status === 'queued') {
      okToast(`已触发 ${d.sources} 个信息源，去画布的执行记录里看进展`);
    } else if (dry) showPreview(d);
    else if (d.status === 'error') errToast(`运行失败：${d.error}`);
    else okToast(`采集 ${d.fetched} 条，发布 ${d.sent} 条${d.failed ? `，失败 ${d.failed} 条` : ''}`);
    render();
  } catch (e) { errToast(e.message); }
  finally { btn.disabled = false; btn.textContent = old; }
}

function showPreview(d) {
  modal({
    title: '试运行结果（未真正发送）', wide: true, dismissible: true,
    body: `<p>采集 <strong>${d.fetched}</strong> 条 · 过滤后保留 <strong>${d.kept}</strong> 条 ·
      模拟发布 <strong>${d.sent}</strong> 条${d.failed ? ` · 失败 <strong>${d.failed}</strong> 条` : ''}</p>
      ${d.error ? `<p class="tag err">${esc(d.error)}</p>` : ''}
      ${(d.items || []).length ? d.items.map((it) => `
        <div class="preview-item">
          <div class="meta">${esc(it.uid || '')} ${it.media.length ? `· ${it.media.length} 个媒体（${esc(it.media.map((m) => m.kind).join('、'))}）` : ''} ${it.url ? `· <a href="${esc(it.url)}" target="_blank">原文</a>` : ''}</div>
          ${esc(it.text || '(无正文)')}
        </div>`).join('') : '<p class="faint">没有产出内容。可能是没有新消息，或全部被过滤规则挡掉了。</p>'}
      ${(d.trace || []).length ? `<h3 class="mt">逐节点轨迹</h3>
        <div class="t-wrap"><table>
          <thead><tr><th>节点</th><th>结果</th><th>产出</th><th>说明</th></tr></thead>
          <tbody>${d.trace.map((t) => `<tr>
            <td>${esc(t.title)} <span class="faint">${esc(t.kind)}</span></td>
            <td>${t.status === 'ok' ? '<span class="tag ok">通过</span>'
              : t.status === 'drop' ? '<span class="tag">终止</span>' : '<span class="tag err">失败</span>'}</td>
            <td class="mono">${t.produced || ''}</td>
            <td class="faint">${esc(t.error || '')}</td></tr>`).join('')}</tbody></table></div>` : ''}
      ${(d.detail || []).filter((x) => x.error).length ? `<h3 class="mt">问题</h3>${d.detail.filter((x) => x.error).map((x) =>
        `<div class="preview-item"><span class="tag err">${esc(x.stage || x.target || '')}</span> ${esc(x.error)}</div>`).join('')}` : ''}`,
  });
}

async function showRuns(id) {
  const rows = await GET(`/api/workflows/${id}/runs`);
  modal({
    title: '运行记录', wide: true, dismissible: true,
    body: rows.length ? `<div class="t-wrap"><table>
      <thead><tr><th>时间</th><th>信息源</th><th>触发</th><th>结果</th><th>采集</th><th>保留</th><th>发布</th><th>失败</th><th>详情</th></tr></thead>
      <tbody>${rows.map((r) => `<tr>
        <td class="nowrap faint">${fmtTime(r.started_at)}</td>
        <td class="faint">${esc(r.source || '—')}</td>
        <td><span class="tag">${r.trigger === 'manual' ? '手动' : r.trigger === 'test' ? '试运行' : '定时'}</span></td>
        <td>${r.status === 'ok' && !r.fetched && !r.kept && !r.sent
          // 采到 0 条的"成功"和发送失败的"成功"在表里长得一样，会被读成静默失败。
          // 这一轮什么都没发生，就直说没有新内容。三个计数都要为 0：只看 fetched
          // 的话，源节点执行记录被清理后重投的那一轮会算出 fetched=0 而 sent=1，
          // 于是显示成"无新内容 · 发布 1"。
          ? '<span class="tag">无新内容</span>' : STATUS_TAG[r.status] || esc(r.status)}</td>
        <td class="mono">${r.fetched}</td><td class="mono">${r.kept}</td>
        <td class="mono">${r.sent}</td><td class="mono">${r.failed || ''}</td>
        <td class="faint" style="max-width:280px;overflow:hidden;text-overflow:ellipsis" title="${esc(r.error || JSON.stringify(r.detail))}">${esc(r.error || '')}</td>
      </tr>`).join('')}</tbody></table></div>` : '<p class="faint">暂无运行记录</p>',
  });
}

// ─────────────────────────────────────────────────────────── 页面：工作流
// 采集、过滤、格式化、输出全部由画布上的节点定义，这里只要一个建工作流的小表单。
function newWorkflowDialog() {
  modal({
    title: '新建工作流',
    body: `
      <div class="field"><label>名称 <span class="req">*</span></label>
        <input data-f="name" data-t="string" placeholder="例如：科技新闻搬运"></div>
      <div class="field"><label>使用账号</label><select data-f="account_id" data-t="int">
        <option value="">— 不指定 —</option>
        ${store.accounts.map((a) => `<option value="${a.id}">${esc(a.name)}${a.status === 'online' ? '' : '（未登录）'}</option>`).join('')}
      </select><div class="help">采集和发送默认都用这个账号，也可以在节点上单独指定</div></div>
      <div class="field"><label>说明</label><input data-f="description" data-t="string"></div>
      <p class="faint">建好之后直接进画布，从「+ 添加节点」开始搭。</p>`,
    footer: '<button data-close>取消</button><button class="primary" data-save>建好并进画布</button>',
    onMount: (root, close) => {
      $('[data-save]', root).onclick = promptToast(async () => {
        const v = readForm(root);
        if (!v.name) return errToast('请填写工作流名称');
        const r = await POST('/api/workflows', { ...v, enabled: false });
        close();
        await refreshStore();
        location.hash = `#/workflows/${r.data.id}/graph`;
      });
    },
  });
}

async function pagePlugins(view) {
  const res = await api('/api/plugins');
  const list = res.data;
  store.plugins = list;
  const byType = { source: [], filter: [], formatter: [], sink: [] };
  list.forEach((p) => (byType[p.type] || []).push(p));
  const TITLES = { source: '信息源', filter: '过滤规则', formatter: '格式化', sink: '输出端' };

  view.innerHTML = `
    <header class="page">
      <div><h1>插件管理</h1><p>把 .py 文件放进 <code>${esc((res.dirs || [])[0] || 'plugins')}</code> 即可热插拔，无需重启。</p></div>
      <div class="spacer"></div>
      <div class="btn-row">
        <button id="upload">上传插件</button>
        <button id="reload">全部重载</button>
      </div>
    </header>
    ${(res.errors || []).length ? `<div class="card" style="border-color:var(--err);margin-bottom:14px">
      <h3 style="color:var(--err)">加载失败的插件</h3>
      ${res.errors.map((e) => `<div class="mono" style="margin-bottom:6px">${esc(e.file)}<br><span class="faint">${esc(e.message)}</span></div>`).join('')}
    </div>` : ''}
    ${Object.keys(byType).map((t) => `
      <div class="card mt">
        <h3>${TITLES[t]} <span class="faint">(${byType[t].length})</span></h3>
        ${byType[t].length ? `<div class="t-wrap"><table>
          <thead><tr><th>名称</th><th>标识</th><th>版本</th><th>说明</th><th>被使用</th><th>状态</th><th></th></tr></thead>
          <tbody>${byType[t].map((p) => `<tr>
            <td>${esc(p.display_name)}${p.requires_account ? ' <span class="tag" title="需要 Telegram 账号">TG</span>' : ''}</td>
            <td class="mono faint">${esc(p.name)}</td>
            <td class="faint">${esc(p.version)}</td>
            <td class="faint" style="max-width:340px">${esc((p.description || '').split('\n')[0])}</td>
            <td>${p.used_by ? `<span class="tag info">${p.used_by} 个工作流</span>` : '<span class="faint">—</span>'}</td>
            <td>${p.enabled ? '<span class="tag ok">启用</span>' : '<span class="tag">禁用</span>'}</td>
            <td class="actions">
              <button class="sm" data-view="${p.name}">源码</button>
              <button class="sm" data-toggle="${p.name}" data-en="${p.enabled}">${p.enabled ? '禁用' : '启用'}</button>
              <button class="sm danger" data-del="${p.name}">删除</button>
            </td></tr>`).join('')}</tbody></table></div>` : '<p class="faint">暂无</p>'}
      </div>`).join('')}`;

  $('#reload').onclick = promptToast(async () => {
    const r = await POST('/api/plugins/reload');
    const d = r.data;
    okToast(`新增 ${d.added.length}，更新 ${d.updated.length}，移除 ${d.removed.length}`);
    if (d.errors.length) errToast(d.errors[0].message);
    await refreshStore(); render();
  });
  $('#upload').onclick = uploadDialog;
  $$('[data-toggle]', view).forEach((b) => b.onclick = promptToast(async () => {
    await POST(`/api/plugins/${b.dataset.toggle}/toggle`, { enabled: b.dataset.en !== 'true' });
    await refreshStore(); render();
  }));
  $$('[data-view]', view).forEach((b) => b.onclick = promptToast(() => sourceEditor(b.dataset.view)));
  $$('[data-del]', view).forEach((b) => b.onclick = () =>
    confirmBox(`删除插件 ${b.dataset.del} 的源文件？此操作不可恢复。`, promptToast(async () => {
      await DEL(`/api/plugins/${b.dataset.del}`); okToast('已删除'); await refreshStore(); render();
    })));
}

async function sourceEditor(name) {
  const d = await GET(`/api/plugins/${name}/source`);
  modal({
    title: `插件源码：${d.file}`, wide: true,
    body: `<textarea id="src" style="min-height:56vh;font-size:12.5px">${esc(d.code)}</textarea>
           <p class="faint">保存后会立即热重载；若有语法错误会自动回滚。</p>`,
    footer: '<button data-close>关闭</button><button class="primary" data-save>保存并重载</button>',
    onMount: (root, close) => {
      $('[data-save]', root).onclick = promptToast(async () => {
        await PUT(`/api/plugins/${name}/source`, { code: $('#src', root).value });
        close(); okToast('已保存并重载'); await refreshStore(); render();
      });
    },
  });
}

function uploadDialog() {
  modal({
    title: '上传插件',
    body: `<div class="field"><label>插件类型（决定放到哪个子目录）</label><select id="up-type">
        <option value="source">信息源 sources/</option>
        <option value="filter">过滤规则 filters/</option>
        <option value="formatter">格式化 formatters/</option>
        <option value="sink">输出端 sinks/</option>
      </select></div>
      <div class="field"><label>选择 .py 文件</label><input type="file" id="up-file" accept=".py"></div>
      <p class="faint">上传后会立即校验并加载；有语法错误会自动删除并提示。</p>`,
    footer: '<button data-close>取消</button><button class="primary" data-save>上传</button>',
    onMount: (root, close) => {
      $('[data-save]', root).onclick = promptToast(async () => {
        const f = $('#up-file', root).files[0];
        if (!f) return errToast('请选择文件');
        const fd = new FormData();
        fd.append('file', f);
        await api(`/api/plugins/upload?type=${$('#up-type', root).value}`, { method: 'POST', body: fd });
        close(); okToast('已上传并加载'); await refreshStore(); render();
      });
    },
  });
}

// ─────────────────────────────────────────────────────────── 页面：账号
async function pageAccounts(view) {
  const list = store.accounts;
  view.innerHTML = `
    <header class="page">
      <div><h1>账号</h1><p>用于采集和发送的 Telegram 账号。api_id / api_hash 在 <a href="https://my.telegram.org" target="_blank">my.telegram.org</a> 申请。</p></div>
      <div class="spacer"></div>
      <div class="btn-row">
        <button id="import">导入 session</button>
        <button class="primary" id="add">+ 添加账号</button>
      </div>
    </header>
    ${apiWarning()}
    <div class="card">
      <div class="t-wrap"><table>
        <thead><tr><th>名称</th><th>类型</th><th>身份</th><th>状态</th><th>代理</th><th></th></tr></thead>
        <tbody>${list.length ? list.map((a) => `<tr>
          <td>${esc(a.name)}<br><span class="faint mono">${esc(a.phone || '')}</span></td>
          <td><span class="tag">${a.is_bot ? 'Bot' : '用户'}</span></td>
          <td class="faint">${a.me && a.me.id ? esc(`${a.me.first_name || ''} @${a.me.username || ''}`) : '—'}</td>
          <td>${statusTag(a)}</td>
          <td class="faint mono">${esc(a.proxy || '—')}</td>
          <td class="actions">
            ${a.status === 'online' && !a.is_bot
              ? `<button class="sm primary" data-code="${a.id}">获取验证码${
                  a.code_watch && a.code_watch.active ? ' ⏳' : ''}</button>` : ''}
            ${a.status === 'online' ? `<button class="sm" data-logout="${a.id}">退出登录</button>`
              : `<button class="sm primary" data-login="${a.id}">登录</button>`}
            <button class="sm" data-edit="${a.id}">编辑</button>
            <button class="sm danger" data-del="${a.id}">删除</button>
          </td></tr>`).join('')
          : `<tr><td colspan="6"><div class="empty"><div class="big">👤</div>还没有账号。<br>可以「+ 添加账号」走验证码登录，也可以直接「导入 session」。</div></td></tr>`}
        </tbody>
      </table></div>
    </div>`;

  bindApiWarning(view);
  $('#add').onclick = () => accountForm(null);
  $('#import').onclick = () => accountForm(null, { tab: 'import' });
  $$('[data-edit]', view).forEach((b) => b.onclick = () => accountForm(list.find((a) => a.id === +b.dataset.edit)));
  $$('[data-login]', view).forEach((b) => b.onclick = () => loginFlow(+b.dataset.login));
  $$('[data-code]', view).forEach((b) => b.onclick = () =>
    codeWatchDialog(list.find((a) => a.id === +b.dataset.code)));
  $$('[data-logout]', view).forEach((b) => b.onclick = () =>
    confirmBox('退出登录会删除本地会话文件，下次需要重新验证。继续？', promptToast(async () => {
      await POST(`/api/accounts/${b.dataset.logout}/logout`); okToast('已退出'); await refreshStore(); render();
    })));
  $$('[data-del]', view).forEach((b) => b.onclick = () =>
    confirmBox('删除该账号？', promptToast(async () => {
      await DEL(`/api/accounts/${b.dataset.del}`); okToast('已删除'); await refreshStore(); render();
    })));
}

function statusTag(a) {
  if (a.status === 'online') return `<span class="tag ok">在线</span>${a.connected ? '' : ' <span class="tag">未连接</span>'}`;
  if (a.status && a.status.startsWith('pending')) return '<span class="tag warn">等待验证</span>';
  if (a.status === 'error') return '<span class="tag err">异常</span>';
  return '<span class="tag">未登录</span>';
}

// 手动填写 / 验证码登录 的表单片段
function manualAccountBody(a, acc) {
  return `<div class="field faint" style="font-size:12px;border:1px solid var(--border);border-radius:7px;padding:8px 11px">
        🔑 使用「设置」里的全局 api_id
        <strong class="mono">${tgApi().configured ? esc(tgApi().api_id) : '（未配置）'}</strong>
        ，所有账号共用一套，这里不用再填。
      </div>
      <div class="form-grid">
        <div class="field"><label>名称 <span class="req">*</span></label><input data-f="name" data-t="string" value="${esc(a.name)}" placeholder="主号"></div>
        <div class="field"><label>手机号</label><input data-f="phone" data-t="string" value="${esc(a.phone)}" placeholder="+8613800138000"></div>
        <div class="field"><label>Bot Token</label><input data-f="bot_token" data-t="string" placeholder="仅 Bot 账号需要"></div>
        <div class="field"><label>代理</label><input data-f="proxy" data-t="string" value="${esc(a.proxy)}" placeholder="socks5://127.0.0.1:1080"></div>
      </div>
      <div class="form-grid">
        <div class="field inline"><input type="checkbox" data-f="is_bot" data-t="bool" ${a.is_bot ? 'checked' : ''}><label>这是一个 Bot</label></div>
        <div class="field inline"><input type="checkbox" data-f="enabled" data-t="bool" ${a.enabled ? 'checked' : ''}><label>启用</label></div>
      </div>
      <p class="faint">保存后回到列表点「登录」，Telegram 会发来验证码。</p>`;
}

// 导入已有 session 的表单片段
function importAccountBody() {
  return `
    <div class="field"><label>来源</label>
      <select id="imp-kind">
        <option value="file">.session 文件</option>
        <option value="string">Session String（粘贴文本）</option>
      </select>
    </div>

    <div id="imp-file-box">
      <div class="field"><label>选择 .session 文件 <span class="req">*</span></label>
        <input type="file" id="imp-file" accept=".session,application/octet-stream">
        <div class="help" id="imp-file-hint">Telethon 生成的 .session（SQLite 格式）。Pyrogram 的不通用。</div></div>
      <div class="field"><label>附带的 .json（可选）</label>
        <input type="file" id="imp-meta" accept=".json,application/json">
        <div class="help">很多账号包会附一个同名 json，里面有 app_id / app_hash，选上就自动填。</div></div>
    </div>

    <div id="imp-string-box" style="display:none">
      <div class="field"><label>Session String <span class="req">*</span></label>
        <textarea id="imp-string" placeholder="1BQAN..." style="min-height:88px"></textarea>
        <div class="help">Telethon 的 StringSession，以 1 开头。</div></div>
    </div>

    <div class="form-grid">
      <div class="field"><label>账号名称 <span class="req">*</span></label>
        <input id="imp-name" placeholder="备用号 1"><div class="help">只是给你自己看的标识</div></div>
      <div class="field"><label>代理</label>
        <input id="imp-proxy" placeholder="socks5://127.0.0.1:1080"></div>
    </div>

    <p class="faint" style="font-size:12px">
      🔑 使用「设置」里的全局 api_id <strong class="mono">${tgApi().configured ? esc(tgApi().api_id) : '（未配置）'}</strong>。
      <strong>这份 session 必须是用同一个 api_id 生成的</strong>，否则 Telegram 会判定密钥无效。<br>
      点下面的按钮会真的连一次 Telegram 验证登录状态，失败不留任何记录。
    </p>
    <div id="imp-result"></div>`;
}

function bindImportForm(root) {
  const kind = $('#imp-kind', root);
  const sync = () => {
    const isFile = kind.value === 'file';
    $('#imp-file-box', root).style.display = isFile ? '' : 'none';
    $('#imp-string-box', root).style.display = isFile ? 'none' : '';
  };
  kind.onchange = sync;
  sync();

  // 选完文件立刻本地体检一次，不用等提交才知道格式不对
  $('#imp-file', root).onchange = async (e) => {
    const f = e.target.files[0];
    const hint = $('#imp-file-hint', root);
    if (!f) return;
    hint.textContent = '正在识别…';
    try {
      const fd = new FormData();
      fd.append('file', f);
      const r = await api('/api/accounts/inspect-session', { method: 'POST', body: fd });
      hint.innerHTML = r.data.usable
        ? `<span class="tag ok">${esc(r.data.detail)}</span>`
        : `<span class="tag err">${esc(r.data.detail)}</span>`;
    } catch (err) {
      hint.innerHTML = `<span class="tag err">${esc(err.message)}</span>`;
    }
  };

  // 选了 json 就把 api_id / api_hash 填进去，省得手抄
  $('#imp-meta', root).onchange = async (e) => {
    const f = e.target.files[0];
    if (!f) return;
    try {
      const d = JSON.parse(await f.text());
      const id = d.app_id ?? d.api_id ?? d.appId ?? d.apiId;
      if (d.phone && !$('#imp-name', root).value) $('#imp-name', root).value = String(d.phone);
      if (id && tgApi().configured && Number(id) !== Number(tgApi().api_id)) {
        errToast(`这份 session 用的是 api_id ${id}，和设置里的 ${tgApi().api_id} 不一致，导入会失败`);
      } else if (id) {
        okToast('json 里的 api_id 与全局设置一致 ✓');
      }
    } catch (_) {
      errToast('这个 json 读不出来，请手动填 api_id / api_hash');
    }
  };
}

async function submitImport(root, close, btn) {
  const kind = $('#imp-kind', root);
  const name = $('#imp-name', root).value.trim();
  if (!name) return errToast('请填写账号名称');
  if (!tgApi().configured) return errToast('请先到「设置」里配置 api_id / api_hash');

  const fd = new FormData();
  fd.append('name', name);
  fd.append('proxy', $('#imp-proxy', root).value.trim());

  if (kind.value === 'file') {
    const f = $('#imp-file', root).files[0];
    if (!f) return errToast('请选择 .session 文件');
    fd.append('file', f);
    const m = $('#imp-meta', root).files[0];
    if (m) fd.append('meta', m);
  } else {
    const str = $('#imp-string', root).value.trim();
    if (!str) return errToast('请粘贴 session string');
    fd.append('session_string', str);
  }

  const label = btn.textContent;
  btn.disabled = true; btn.textContent = '正在连接 Telegram…';
  $('#imp-result', root).innerHTML = '';
  try {
    const r = await api('/api/accounts/import', { method: 'POST', body: fd });
    const me = r.data.me || {};
    close();
    okToast(`已导入：${me.first_name || ''} ${me.username ? '@' + me.username : ''} ${me.phone ? '(' + me.phone + ')' : ''}`.trim());
    await refreshStore(); render();
  } catch (e) {
    $('#imp-result', root).innerHTML =
      `<div class="preview-item"><span class="tag err">导入失败</span><br>${esc(e.message)}</div>`;
    throw e;
  } finally {
    btn.disabled = false; btn.textContent = label;
  }
}

// 新建账号：手动填写 / 导入 session 两个页签
function accountForm(acc, opts) {
  const a = acc || { name: '', api_id: '', api_hash: '', phone: '', is_bot: false, bot_token: '', proxy: '', enabled: true };

  // 编辑已有账号时没有页签，就是一张表
  if (acc) {
    modal({
      title: '编辑账号',
      body: manualAccountBody(a, acc),
      footer: '<button data-close>取消</button><button class="primary" data-save>保存</button>',
      onMount: (root, close) => {
        $('[data-save]', root).onclick = promptToast(async () => {
          const v = readForm(root);
          if (!v.api_hash) delete v.api_hash;
          if (!v.bot_token) delete v.bot_token;
          await PATCH(`/api/accounts/${acc.id}`, v);
          close(); okToast('已保存'); await refreshStore(); render();
        });
      },
    });
    return;
  }

  let tab = (opts && opts.tab) || 'manual';
  modal({
    title: '添加账号', wide: true,
    body: `
      <div class="btn-row" id="acc-tabs" style="margin-bottom:14px">
        <button data-tab="manual">① 手机号 + 验证码登录</button>
        <button data-tab="import">② 导入已登录的 session</button>
      </div>
      <div id="acc-manual">${manualAccountBody(a, null)}</div>
      <div id="acc-import">${importAccountBody()}</div>`,
    footer: '<button data-close>取消</button><button class="primary" data-save></button>',
    onMount: (root, close) => {
      const save = $('[data-save]', root);
      const paint = () => {
        $$('#acc-tabs button', root).forEach((b) =>
          b.className = b.dataset.tab === tab ? 'primary' : '');
        $('#acc-manual', root).style.display = tab === 'manual' ? '' : 'none';
        $('#acc-import', root).style.display = tab === 'import' ? '' : 'none';
        save.textContent = tab === 'manual' ? '保存' : '验证并导入';
      };
      $$('#acc-tabs button', root).forEach((b) => b.onclick = () => { tab = b.dataset.tab; paint(); });
      bindImportForm(root);
      paint();

      save.onclick = promptToast(async () => {
        if (tab === 'import') return submitImport(root, close, save);
        const v = readForm($('#acc-manual', root));
        if (!v.name) return errToast('请填写账号名称');
        if (!tgApi().configured) return errToast('请先到「设置」里配置 api_id / api_hash');
        await POST('/api/accounts', v);
        close(); okToast('已保存，回到列表点「登录」收验证码'); await refreshStore(); render();
      });
    },
  });
}

// 监听该账号收到的登录验证码（在别处登录这个号时用）
function codeWatchDialog(acc) {
  if (!acc) return;
  let timer = null;
  let stopped = false;

  const paint = (d) => {
    const box = $('#cw-body');
    if (!box) return;
    const codes = d.codes || [];
    box.innerHTML = `
      <div style="display:flex;align-items:center;gap:10px;margin-bottom:12px">
        ${d.active
          ? `<span class="tag ok">监听中</span><span class="faint">剩余 ${d.remaining} 秒</span>`
          : `<span class="tag">${codes.length ? '监听已结束' : '监听已结束，没等到验证码'}</span>`}
      </div>
      ${codes.length ? codes.map((c, i) => `
        <div class="preview-item" style="display:flex;align-items:center;gap:12px">
          <span style="font-size:26px;font-weight:600;letter-spacing:3px;font-family:var(--mono)">${esc(c.code)}</span>
          <button class="sm" data-copy="${esc(c.code)}">复制</button>
          <span class="spacer" style="flex:1"></span>
          <span class="faint" style="font-size:11px;text-align:right">
            ${i === 0 ? '<span class="tag ok">最新</span><br>' : ''}
            ${esc(c.source === 'history' ? '开监听前已收到' : '刚刚收到')} · ${fmtTime(c.at)}
          </span>
        </div>`).join('')
        : `<div class="empty" style="padding:26px">
             <div class="big">${d.active ? '📡' : '🕐'}</div>
             ${d.active
               ? '正在等待验证码…<br><span class="faint">现在去另一台设备/程序上登录这个号，码到了会直接显示在这里</span>'
               : '这段时间内没有收到验证码。<br><span class="faint">确认一下是否真的触发了登录，或者再监听一次。</span>'}
           </div>`}
      ${d.error ? `<div class="preview-item"><span class="tag err">读取失败</span><br>${esc(d.error)}</div>` : ''}
      ${(d.messages || []).length ? `
        <details ${codes.length ? '' : 'open'} style="margin-top:10px">
          <summary class="faint" style="cursor:pointer;font-size:12px">
            服务号最近 ${d.messages.length} 条消息${codes.length ? '' : '（没抽出验证码时可以自己看）'}
          </summary>
          ${d.messages.map((m) => `
            <div class="preview-item" style="font-size:12px">
              <div class="meta">${fmtTime(m.at)} · ${esc(m.source === 'history' ? '历史' : '实时')}</div>
              ${esc(m.text || '(空消息)')}
            </div>`).join('')}
        </details>` : ''}`;

    $$('[data-copy]', box).forEach((b) => b.onclick = async () => {
      try {
        await navigator.clipboard.writeText(b.dataset.copy);
        okToast('已复制 ' + b.dataset.copy);
      } catch (_) {
        errToast('复制失败，请手动选中');
      }
    });

    const restart = $('#cw-restart');
    if (restart) restart.style.display = d.active ? 'none' : '';
  };

  const poll = async () => {
    if (stopped) return;
    try {
      paint(await GET(`/api/accounts/${acc.id}/code-watch`));
    } catch (_) { /* 弹窗可能已经关了 */ }
  };

  const begin = async () => {
    paint({ active: true, remaining: 0, codes: [] });
    try {
      paint(await POST(`/api/accounts/${acc.id}/code-watch`, { seconds: 180 })
        .then((r) => r.data));
    } catch (e) {
      errToast(e.message);
      paint({ active: false, codes: [], error: e.message });
      return;
    }
    clearInterval(timer);
    timer = setInterval(poll, 2000);
  };

  modal({
    title: `获取登录验证码 — ${acc.name}`,
    body: `
      <p class="faint" style="margin-top:0">
        这个号已经登录在 TeleOps 里，所以它收到的登录验证码可以直接读出来。<br>
        <strong>先点开始监听，再去另一台设备/程序上登录这个号。</strong>
      </p>
      <div id="cw-body"></div>`,
    footer: `<button id="cw-restart" style="display:none">再监听 3 分钟</button>
             <button data-close>关闭</button>`,
    onMount: (root) => {
      $('#cw-restart', root).onclick = begin;

      // 标题栏的 ✕ 和页脚的「关闭」都要接管——两个都带 data-close，
      // 只改第一个的话，从另一个关闭就会把监听留在后台跑。
      const teardown = async (origClose) => {
        stopped = true;
        clearInterval(timer);
        try { await DEL(`/api/accounts/${acc.id}/code-watch`); } catch (_) { /* 无所谓 */ }
        if (origClose) origClose();
        await refreshStore();
        if ((location.hash || '#/') === '#/accounts') render();   // 刷掉行上的监听中标记
      };
      $$('[data-close]', root).forEach((b) => {
        const orig = b.onclick;
        b.onclick = () => teardown(orig);
      });
      // 这个弹窗不响应点遮罩/Esc，关闭只有上面两个按钮，teardown 一定跑得到。
      begin();
    },
  });
}

function loginFlow(id) {
  let closeStep = null;
  const step = (stage, hint) => {
    if (closeStep) closeStep();          // 每一步是一个新弹窗，先撤掉上一步的
    if (stage === 'done') { okToast('登录成功'); refreshStore().then(render); return; }
    closeStep = modal({
      title: stage === 'code' ? '输入验证码' : '输入两步验证密码',
      body: `<p class="faint">${esc(hint || '')}</p>
             <div class="field"><label>${stage === 'code' ? '验证码（发送到你的 Telegram 客户端）' : '两步验证密码'}</label>
             <input id="lg-val" type="${stage === 'code' ? 'text' : 'password'}" autocomplete="off"></div>`,
      footer: '<button data-close>取消</button><button class="primary" data-ok>提交</button>',
      onMount: (root) => {
        const input = $('#lg-val', root);
        input.focus();
        const submit = promptToast(async () => {
          const url = stage === 'code' ? `/api/accounts/${id}/code` : `/api/accounts/${id}/password`;
          const body = stage === 'code' ? { code: input.value } : { password: input.value };
          const r = await POST(url, body);
          step(r.data.stage, r.data.hint);
        });
        $('[data-ok]', root).onclick = submit;
        input.onkeydown = (e) => { if (e.key === 'Enter') submit(); };
      },
    });
  };
  promptToast(async () => {
    const r = await POST(`/api/accounts/${id}/login`);
    step(r.data.stage, r.data.hint);
  })();
}

// ─────────────────────────────────────────────────────────── 页面：设置
async function pageSettings(view) {
  const d = await GET('/api/settings');
  store.settings = d;
  const t = d.telegram, sys = d.system;

  view.innerHTML = `
    <header class="page">
      <div><h1>设置</h1><p>全局配置。api_id / api_hash 由所有账号共用。</p></div>
    </header>

    <div class="card">
      <h3>Telegram API</h3>
      <p class="faint" style="margin-top:0">
        在 <a href="https://my.telegram.org" target="_blank">my.telegram.org</a> →
        API development tools 申请，填一次即可，所有账号共用。
      </p>
      <div class="form-grid">
        <div class="field"><label>api_id <span class="req">*</span></label>
          <input type="number" id="set-apiid" value="${t.api_id || ''}" placeholder="1234567"></div>
        <div class="field"><label>api_hash ${t.has_hash ? '' : '<span class="req">*</span>'}</label>
          <input id="set-apihash" placeholder="${t.has_hash ? '已保存，留空则不修改' : '32 位十六进制'}">
          <div class="help">${t.has_hash ? `当前：<span class="mono">${esc(t.api_hash_masked)}</span>` : '尚未设置'}</div></div>
      </div>
      <div class="btn-row">
        <button class="primary" id="set-save">保存</button>
        <span class="faint" style="align-self:center">
          ${t.configured ? '<span class="tag ok">已配置</span>' : '<span class="tag err">未配置</span>'}
          · 当前 ${d.accounts.online}/${d.accounts.total} 个账号在线
        </span>
      </div>
      <p class="faint mt" style="font-size:12px;margin-bottom:0">
        ⚠️ 账号的登录凭证（session）绑定在生成它的那个 api_id 上。
        <strong>改动 api_id 会让已登录的账号失效</strong>，需要重新登录或重新导入。
        没有特殊原因不要改。
      </p>
    </div>

    <div class="card mt">
      <h3>系统信息</h3>
      <div class="t-wrap"><table><tbody>
        ${[
          ['版本', sys.version],
          ['已运行', fmtDuration(sys.uptime)],
          ['后台口令', sys.auth_enabled ? '已开启' : '未开启（仅本机访问）'],
          ['数据目录', sys.data_dir],
          ['会话目录', sys.sessions_dir],
          ['媒体目录', sys.media_dir],
          ['插件目录', sys.plugin_dirs.join('、')],
          ['插件热重载', sys.auto_reload ? '开启' : '关闭'],
          ['全局发送间隔', sys.send_interval + ' 秒'],
          ['单轮最多处理', sys.max_items_per_run + ' 条'],
          ['最大可等限流', sys.max_flood_wait + ' 秒'],
          ['媒体保留', sys.media_retention_days > 0 ? sys.media_retention_days + ' 天' : '用完即删'],
        ].map(([k, v]) => `<tr><td class="faint" style="width:130px">${esc(k)}</td>
             <td class="mono" style="word-break:break-all">${esc(v)}</td></tr>`).join('')}
      </tbody></table></div>
      <p class="faint" style="font-size:12px;margin-bottom:0">
        这些来自 <code>config.yaml</code>，改动后需重启服务生效。
      </p>
    </div>`;

  $('#set-save').onclick = promptToast(async () => {
    const btn = $('#set-save');
    const apiId = $('#set-apiid').value.trim();
    const apiHash = $('#set-apihash').value.trim();
    if (!apiId) return errToast('请填写 api_id');
    if (!t.has_hash && !apiHash) return errToast('请填写 api_hash');

    const doSave = async () => {
      btn.disabled = true;
      try {
        const r = await PUT('/api/settings/telegram', { api_id: +apiId, api_hash: apiHash });
        if (r.data.warning) errToast(r.data.warning); else okToast('已保存');
        await refreshStore(); render();
      } finally { btn.disabled = false; }
    };

    // 改动会踢掉已登录的账号，先问一句
    const changing = t.configured && (+apiId !== +t.api_id || !!apiHash);
    if (changing && d.accounts.online > 0) {
      confirmBox(
        `当前有 ${d.accounts.online} 个账号处于登录状态。改动 api_id / api_hash 会让它们的 session 失效，` +
        '需要重新登录或重新导入。确定要改吗？',
        doSave);
    } else {
      await doSave();
    }
  });
}

// ─────────────────────────────────────────────────────────── 页面：日志
let logTimer = null;
async function pageLogs(view) {
  view.innerHTML = `
    <header class="page">
      <div><h1>运行日志</h1><p>实时输出，每 3 秒自动刷新。</p></div>
      <div class="spacer"></div>
      <div class="btn-row">
        <select id="lv" style="width:auto">
          <option value="">全部级别</option><option value="INFO">INFO</option>
          <option value="WARNING">WARNING</option><option value="ERROR">ERROR</option>
        </select>
        <button id="pause">暂停刷新</button>
      </div>
    </header>
    <div class="card"><div class="log-view" id="logbox">加载中…</div></div>`;

  let paused = false;
  const load = async () => {
    if (paused) return;
    try {
      const rows = await GET(`/api/logs?limit=300${$('#lv').value ? `&level=${$('#lv').value}` : ''}`);
      $('#logbox').innerHTML = rows.length ? rows.map((r) =>
        `<div class="log-line"><span class="ts">${esc(r.ts)}</span> <span class="lv-${r.level}">${r.level.padEnd(7)}</span> <span class="nm">${esc(r.name)}</span> ${esc(r.msg)}</div>`
      ).join('') : '<span class="faint">暂无日志</span>';
    } catch (e) { /* 页面可能已切换 */ }
  };
  $('#lv').onchange = load;
  $('#pause').onclick = (e) => { paused = !paused; e.target.textContent = paused ? '继续刷新' : '暂停刷新'; if (!paused) load(); };
  await load();
  clearInterval(logTimer);
  logTimer = setInterval(() => { if (location.hash === '#/logs') load(); else clearInterval(logTimer); }, 3000);
}

// ─────────────────────────────────────────────────────────── 路由
const ROUTES = [
  { hash: '#/', icon: '📊', name: '概览', page: pageOverview },
  { hash: '#/channels', icon: '📢', name: '频道管理', page: pageChannels, count: () => store.channels.length },
  { hash: '#/workflows', icon: '⚙️', name: '工作流', page: pageWorkflows, count: () => store.workflows.length },
  { hash: '#/plugins', icon: '🧩', name: '插件管理', page: pagePlugins, count: () => store.plugins.length },
  { hash: '#/accounts', icon: '👤', name: '账号', page: pageAccounts, count: () => store.accounts.length },
  { hash: '#/logs', icon: '📜', name: '日志', page: pageLogs },
  { hash: '#/settings', icon: '🔧', name: '设置', page: pageSettings },
];

function paintNav() {
  const cur = location.hash || '#/';
  $('#nav').innerHTML = ROUTES.filter((r) => !r.hidden).map((r) => `
    <a href="${r.hash}" class="${(r.match ? r.match(cur) : r.hash === cur) ? 'active' : ''}">
      <span>${r.icon}</span><span>${r.name}</span>
      ${r.count ? `<span class="badge">${r.count()}</span>` : ''}
    </a>`).join('');
}

let lastHash = null;

async function render() {
  const h = location.hash || '#/';
  // 概览页每 20 秒自己 render 一次。入场动画只在真的换了页时播，
  // 否则就成了每 20 秒闪一下的频闪灯
  const changed = h !== lastHash;
  lastHash = h;
  const route = ROUTES.find((r) => (r.match ? r.match(h) : r.hash === h)) || ROUTES[0];
  const view = $('#view');
  paintNav();
  try {
    await route.page(view);
    ANIM.pageEnter(view, changed);
  } catch (e) {
    view.innerHTML = `<div class="card"><div class="empty"><div class="big">⚠️</div>${esc(e.message)}</div></div>`;
  }
  paintNav();
}

// 供 graph.js 之类的附加脚本使用。这个文件整体在 IIFE 里，不导出就什么都调不到。
window.TeleOps = {
  $, $$, esc, api, GET, POST, PUT, PATCH, DEL,
  store, refreshStore, modal, confirmBox, closeModal,
  fieldHtml, schemaForm, readForm,
  toast, okToast, errToast, promptToast, fmtTime,
  render: () => render(),
  registerRoute: (route) => { ROUTES.push(route); },
  anim: ANIM,
};

async function boot() {
  try {
    const ping = await fetch('/api/ping').then((r) => r.json());
    if (ping.auth && !token()) promptToken();
    await refreshStore();
    $('#foot').innerHTML = `TeleOps v0.1.0<br>插件 ${store.plugins.length} 个已加载`;
  } catch (e) {
    $('#health').style.background = 'var(--err)';
    $('#foot').textContent = '连接后端失败';
  }
  window.addEventListener('hashchange', render);
  await render();
  setInterval(() => { if ((location.hash || '#/') === '#/') render(); }, 20000);
}

document.addEventListener('DOMContentLoaded', boot);
})();
