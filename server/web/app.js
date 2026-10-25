"use strict";

// ------------------------------------------------------------ 常量与工具

const STATUS = {
  new: ["未连接", "grey"],
  offline: ["离线", "red"],
  skew: ["时间不一致", "yellow"],
  syncing: ["同步中", "blue"],
  synced: ["已同步", "green"],
  error: ["失败", "yellow"],
  deleting: ["删除中", "grey"],
};
const LEVEL = { ok: "正常", fault: "故障", lost: "失联", none: "无数据" };
const LIVE = new Set(["syncing", "deleting"]);   // 带呼吸动画的状态
const REFRESH_MS = 5000;
const LOGO = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M4 8h14l-3.5-3.5M20 16H6l3.5 3.5"/></svg>`;

const $ = (sel, root = document) => root.querySelector(sel);
const app = $("#app");

let me = null;                    // {username, must_change}
let currentView = null;           // 当前页面的渲染函数
let refreshTimer = null;
let currentNode = null;           // 详情页的节点
let chainsById = {};              // 转发列表，编辑时使用
let sortMode = null;              // 正在排序的列表："nodes" / "chains"
const openDetails = new Set();    // 已展开的折叠区，刷新后保持
const revealedKeys = new Set();   // 已显示密钥的节点

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}

const pad = (n) => String(n).padStart(2, "0");

function clock(ts) {
  const d = new Date(ts * 1000);
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

function fmtTime(ts) {
  if (!ts) return "-";
  const d = new Date(ts * 1000);
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${clock(ts)}`;
}

function ago(ts, now) {
  if (!ts) return "从未上报";
  const d = Math.max(0, now - ts);
  if (d < 60) return `${d} 秒前`;
  if (d < 3600) return `${Math.floor(d / 60)} 分钟前`;
  if (d < 86400) return `${Math.floor(d / 3600)} 小时前`;
  return `${Math.floor(d / 86400)} 天前`;
}

function dot(status) {
  const [label, color] = STATUS[status] || [status, "grey"];
  return `<i class="dot c-${color}${LIVE.has(status) ? " live" : ""}" title="${label}"></i>`;
}

function pill(status) {
  const [label, color] = STATUS[status] || [status, "grey"];
  return `<span class="pill t-${color}">${dot(status)}${label}</span>`;
}

// 节点名称下方的小字：入站地址 · 备注（单行，过长显示省略号）
function nodeSub(n) {
  const note = n.note ? `<span class="sep">·</span><span class="note" title="${esc(n.note)}">${esc(n.note.replace(/\n+/g, " "))}</span>` : "";
  return `<div class="sub node-sub"><span class="mono">${esc(n.addr)}</span>${note}</div>`;
}

// 页脚：登录后显示版本号，登录页只显示名称
function footer(withVersion) {
  $("#footer-title").textContent = withVersion && me?.version
    ? `ictl v${me.version} · 轻量的中心化 iptables 端口转发管理` : "ictl";
}

function emptyState(icon, text) {
  return `<div class="empty"><div class="empty-icon">${icon}</div>${text}</div>`;
}

// 渲染页面：切换页面时整体替换并播放进入动画；刷新时只更新有变化的部分，
// 已有元素保持不变，不会重放动画、丢失悬停状态或文字选区
let fullRender = true;
let enterTimer = null;

function render(html) {
  const tpl = document.createElement("template");
  tpl.innerHTML = html;
  tpl.content.querySelectorAll(".logo").forEach((el) => (el.innerHTML = LOGO));
  if (!fullRender && app.firstChild) {
    morph(app, tpl.content);
    return;
  }
  fullRender = false;
  app.replaceChildren(tpl.content);
  app.classList.remove("view-enter");
  void app.offsetWidth;
  app.classList.add("view-enter");
  clearTimeout(enterTimer);
  enterTimer = setTimeout(() => app.classList.remove("view-enter"), 600);
}

// 用新节点树更新现有节点的子节点：类型、标签或 data-key 不同时替换，否则就地更新
function morph(from, to) {
  const olds = [...from.childNodes];
  const news = [...to.childNodes];
  news.forEach((nb, i) => {
    const na = olds[i];
    if (!na) return from.appendChild(nb);
    if (na.nodeName !== nb.nodeName || (na.nodeType === 1 && na.dataset.key !== nb.dataset.key)) {
      return from.replaceChild(nb, na);
    }
    if (na.nodeType !== 1) {
      if (na.nodeValue !== nb.nodeValue) na.nodeValue = nb.nodeValue;
      return;
    }
    for (const { name } of [...na.attributes]) if (!nb.hasAttribute(name)) na.removeAttribute(name);
    for (const { name, value } of [...nb.attributes]) if (na.getAttribute(name) !== value) na.setAttribute(name, value);
    morph(na, nb);
  });
  for (let i = news.length; i < olds.length; i++) olds[i].remove();
}

function bar(b) {
  const cells = b.levels.map((lv, i) => {
    const t = b.start + i * b.window;
    return `<span class="lv-${lv}" title="${clock(t)} – ${clock(t + b.window)}  ${LEVEL[lv]}"></span>`;
  });
  const minutes = Math.round((b.levels.length * b.window) / 60);
  return `<div class="bar">${cells.join("")}</div><div class="bar-axis"><span>${minutes} 分钟前</span><span>现在</span></div>`;
}

function toast(text, error = false) {
  const t = $("#toast");
  t.textContent = text;
  t.className = error ? "error" : "";
  t.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => (t.hidden = true), error ? 4000 : Math.max(2000, text.length * 100));
}

async function copy(text) {
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    // HTTP 页面没有剪贴板 API，退回旧方法
    const t = document.createElement("textarea");
    t.value = text;
    t.style.position = "fixed";
    t.style.opacity = "0";
    document.body.append(t);
    t.select();
    document.execCommand("copy");
    t.remove();
  }
  toast("已复制");
}

function syncAddress(publicUrl, syncPath) {
  return (publicUrl || location.origin) + syncPath;
}

// ------------------------------------------------------------ 接口

class ApiError extends Error {
  constructor(status, data) {
    super(data.error || `请求失败（${status}）`);
    this.status = status;
    this.field = data.field;
    this.data = data;
  }
}

async function api(method, path, body) {
  const opt = { method, headers: {}, credentials: "same-origin" };
  if (method !== "GET") {
    opt.headers["Content-Type"] = "application/json";
    opt.body = JSON.stringify(body || {});
  }
  let res;
  try {
    res = await fetch("api/" + path, opt);
  } catch {
    throw new ApiError(0, { error: "无法连接服务端" });
  }
  let data = {};
  try {
    data = await res.json();
  } catch {}
  if (res.ok) return data;
  if (res.status === 401 && path !== "login") {
    me = null;
    showLogin();
  } else if (res.status === 403 && data.must_change) {
    me = { ...me, must_change: true };
    showChangeAccount();
  }
  throw new ApiError(res.status, data);
}

// 执行异步操作，统一处理错误
async function safe(fn) {
  try {
    return await fn();
  } catch (e) {
    if (!(e instanceof ApiError)) throw e;
    if (e.status === 401 || (e.status === 403 && e.data.must_change)) return;
    toast(e.message, true);
  }
}

// ------------------------------------------------------------ 页面切换与刷新

async function show(view, auto) {
  currentView = view;
  view.auto = auto;
  fullRender = true;
  await safe(view);
  schedule();
}

function schedule() {
  clearTimeout(refreshTimer);
  if (!currentView?.auto) return;
  refreshTimer = setTimeout(async () => {
    if ($("#modal").hidden && !document.hidden && !sortMode) await safe(currentView);
    schedule();
  }, REFRESH_MS);
}

function refresh() {
  return safe(currentView);
}

async function route() {
  clearTimeout(refreshTimer);
  closeModal();
  sortMode = null;
  if (!me) {
    try {
      me = await api("GET", "me");
    } catch {
      return;
    }
  }
  if (me.must_change) return showChangeAccount();

  document.body.classList.remove("auth-page");
  footer(true);
  $("#nav").hidden = false;
  const [page, id] = (location.hash || "#/nodes").replace(/^#\/?/, "").split("/");
  document.querySelectorAll("[data-nav]").forEach((a) => a.classList.toggle("active", a.dataset.nav === page));

  if (page === "nodes" && id) return show(() => nodeDetail(Number(id)), true);
  if (page === "nodes") return show(nodeList, true);
  if (page === "chains") return show(chainList, true);
  if (page === "settings") return show(settingsPage, false);
  location.hash = "#/nodes";
}

// ------------------------------------------------------------ 登录与账号

function showLogin() {
  clearTimeout(refreshTimer);
  closeModal();
  if (document.body.classList.contains("auth-page") && $('#app .auth[data-form="login"]')) return;   // 已显示时不重建，保留已输入内容
  $("#nav").hidden = true;
  document.body.classList.add("auth-page");
  footer(false);
  fullRender = true;
  render(`
    <form class="auth panel" data-form="login">
      <i class="logo"></i>
      <h1>ictl</h1>
      <p>登录管理面板</p>
      <label><span>用户名</span><input name="username" autocomplete="username" required autofocus></label>
      <label><span>密码</span><input name="password" type="password" autocomplete="current-password" required></label>
      <div class="form-error"></div>
      <button class="primary">登录</button>
    </form>`);
}

function accountFields(username, first) {
  const req = first ? "required" : `placeholder="留空则不修改"`;
  return `
    <label><span>当前密码</span><input name="current_password" type="password" autocomplete="current-password" required></label>
    <label><span>用户名</span><input name="username" value="${esc(username)}" autocomplete="username" required></label>
    <label><span>新密码</span><input name="password" type="password" autocomplete="new-password" minlength="8" ${req}></label>
    <label><span>确认新密码</span><input name="password2" type="password" autocomplete="new-password" ${req}></label>
    <div class="form-error"></div>`;
}

function showChangeAccount() {
  clearTimeout(refreshTimer);
  closeModal();
  if (document.body.classList.contains("auth-page") && $('#app .auth[data-form="account"]')) return;   // 已显示时不重建，保留已输入内容
  $("#nav").hidden = true;
  document.body.classList.add("auth-page");
  footer(false);
  fullRender = true;
  render(`
    <form class="auth panel" data-form="account">
      <i class="logo"></i>
      <h1>修改默认账号</h1>
      <p>首次登录需要修改用户名和密码，两者都不能使用默认值。</p>
      ${accountFields("", true)}
      <button class="primary">保存并进入</button>
      <div class="form-actions"><button type="button" class="link" data-action="logout">退出登录</button></div>
    </form>`);
}

// ------------------------------------------------------------ 节点列表

// 列表页标题栏：排序模式下显示「取消 / 完成」
function listHead(title, count, kind, newAction, newLabel) {
  const buttons = sortMode === kind
    ? `<button data-action="sort-cancel">取消</button>
       <button class="primary" data-action="sort-save" data-kind="${kind}">完成</button>`
    : `${count > 1 ? `<button data-action="sort-start" data-kind="${kind}">排序</button>` : ""}
       <button class="primary" data-action="${newAction}">${newLabel}</button>`;
  return `<div class="page-head"><h1>${title}<span class="count">${count || ""}</span></h1>
    <div class="head-actions">${buttons}</div></div>`;
}

// 排序模式：精简的卡片，左侧为拖动手柄
function sortList(kind, items) {
  return `<p class="hint sort-hint">按住左侧手柄拖动调整顺序，点「完成」保存。</p>
    <div class="sort-list">${items.map(([id, inner]) => `
      <div class="card sort-card" data-key="${kind}-${id}" data-id="${id}">
        <span class="drag-handle" title="拖动排序">⠿</span><div class="sort-body">${inner}</div>
      </div>`).join("")}</div>`;
}

async function nodeList() {
  const d = await api("GET", "nodes");
  if (sortMode === "nodes") {
    return render(listHead("节点", d.nodes.length, "nodes") +
      sortList("node", d.nodes.map((n) => [n.id, `<div class="title">${esc(n.name)}</div>${nodeSub(n)}`])));
  }
  const cards = d.nodes.map((n) => `
    <a class="card" href="#/nodes/${n.id}" data-key="node-${n.id}">
      <div class="card-head">
        <div class="node-head"><div class="title">${esc(n.name)}</div>${nodeSub(n)}</div>
        <div class="meta">${pill(n.status)}<span class="muted">${ago(n.last_seen, d.now)}</span></div>
      </div>
      ${bar(n.bar)}
    </a>`);
  render(`
    ${listHead("节点", d.nodes.length, "nodes", "node-new", "新增节点")}
    ${cards.join("") || emptyState("◎", "还没有节点，点击右上角新增。")}`);
}

// ------------------------------------------------------------ 节点详情

function detailsBlock(key, title, content) {
  return `<details data-key="${key}" ${openDetails.has(key) ? "open" : ""}><summary>${title}</summary>${content}</details>`;
}

async function nodeDetail(id) {
  let d;
  try {
    d = await api("GET", `nodes/${id}`);
  } catch (e) {
    if (e instanceof ApiError && e.status === 404) {
      location.hash = "#/nodes";
      return;
    }
    throw e;
  }
  const n = (currentNode = d.node);
  const url = syncAddress(d.public_url, d.sync_path);
  const shown = revealedKeys.has(id);
  const install = (key) => `curl -fsSL ${d.agent_url} -o ictl.sh && sudo bash ictl.sh --url '${url}' --key '${key}'`;

  const rules = d.rules.length
    ? `<table><thead><tr><th>入站端口</th><th>转发到</th><th>所属转发</th></tr></thead><tbody>${d.rules.map((r) => `
        <tr><td class="mono">${r.port}</td><td class="mono">${esc(r.host)}:${r.tport}</td>
        <td><a href="#/chains">${esc(r.chain_remark)}</a></td></tr>`).join("")}</tbody></table>`
    : `<div class="muted">${n.status === "deleting" ? "正在删除，已下发空配置" : "没有转发规则"}</div>`;

  const skew = n.skew == null ? "-" : `${n.skew > 0 ? "+" : ""}${n.skew} 秒`;
  const more = `<div class="kv-grid">
    <span>来源 IP</span><span class="mono">${esc(n.src_ip || "-")}</span>
    <span>时差</span><span>${skew}</span>
    <span>节点端版本</span><span>${esc(n.agent || "-")}${n.agent && n.agent !== d.version
      ? `<span class="muted">（服务端为 ${esc(d.version)}，可在节点上运行 <code>sudo ictl --update</code> 更新）</span>` : ""}</span>
    <span>已应用版本</span><span class="mono">${esc(n.reported_v || "-")}</span>
    <span>应有版本</span><span class="mono">${esc(n.want_v)}</span>
    <span>上报状态</span><span>${esc(n.state || "-")}</span>
    <span>错误信息</span><span>${esc(n.err || "-")}</span>
    <span>首次连接</span><span>${fmtTime(n.first_seen)}</span>
    <span>使用该节点的转发</span><span>${d.chains.map((c) => esc(c.remark)).join("、") || "-"}</span>
  </div>`;

  const events = d.events.length
    ? `<ul class="events">${d.events.map((e) => `<li><time>${fmtTime(e.ts)}</time><span>${esc(e.text)}</span></li>`).join("")}</ul>`
    : `<div class="muted" style="padding-top:10px">暂无事件</div>`;

  render(`
    <a class="back" href="#/nodes">← 节点列表</a>
    <div class="detail-head">
      <div class="node-head"><h1>${esc(n.name)}</h1><div class="sub mono">${esc(n.addr)}</div></div>
      <div class="meta">${pill(n.status)}<span class="muted">最后上报 ${ago(n.last_seen, d.now)}</span></div>
    </div>
    ${n.note ? `<div class="panel note-full"><h2>备注</h2><div>${esc(n.note)}</div></div>` : ""}
    <div class="panel">${bar(n.bar)}</div>
    <div class="panel">
      <h2>接入信息</h2>
      <div class="kv"><span>同步地址</span><code>${esc(url)}</code>
        <button class="small" data-action="copy" data-text="${esc(url)}">复制</button></div>
      <div class="kv"><span>密钥</span><code>${shown ? esc(n.key) : "•".repeat(32)}</code>
        <button class="small" data-action="key-toggle">${shown ? "隐藏" : "显示"}</button>
        <button class="small" data-action="copy" data-text="${esc(n.key)}">复制</button></div>
      <div class="kv"><span>一键接入</span><code>${esc(install(shown ? n.key : "••••••"))}</code>
        <button class="small" data-action="copy" data-text="${esc(install(n.key))}">复制</button></div>
      <p class="hint">在节点上执行一键接入命令即可完成安装和接入；也可以运行 <code>sudo ictl</code>，选择 1 手动填写同步地址和密钥。</p>
    </div>
    <div class="panel"><h2>转发规则<span class="count">允许端口 ${esc(n.ports)}</span></h2>${rules}</div>
    <div class="actions">
      <button data-action="node-edit">编辑</button>
      <button data-action="node-rekey">重置密钥</button>
      ${n.status === "deleting"
        ? `<button class="danger" data-action="node-delete" data-force="1">强制删除</button>`
        : `<button class="danger" data-action="node-delete">删除</button>`}
    </div>
    ${detailsBlock(`more-${id}`, "更多信息", more)}
    ${detailsBlock(`events-${id}`, "事件记录", events)}`);
}

// ------------------------------------------------------------ 转发

// 等待确认的节点，附带离线、异常等状态
function pendingLine(c) {
  if (!c.pending.length) return "";
  const who = c.pending.map((p) => {
    const note = ["synced", "syncing"].includes(p.status) ? "" : `<span class="muted">（${STATUS[p.status]?.[0] || p.status}）</span>`;
    return `<span class="who">${dot(p.status)}${esc(p.name)}${note}</span>`;
  });
  return `<div class="pending">等待确认：${who.join("")}</div>`;
}

async function chainList() {
  const d = await api("GET", "chains");
  chainsById = Object.fromEntries(d.chains.map((c) => [c.id, c]));
  if (sortMode === "chains") {
    return render(listHead("转发", d.chains.length, "chains") + sortList("chain", d.chains.map((c) => [c.id,
      `<div class="title">${esc(c.remark)}</div>
       <div class="sub">${c.hops.map((h) => esc(h.name)).join(" → ")} → <span class="mono">${esc(c.target_host)}:${c.target_port}</span></div>`])));
  }
  const cards = d.chains.map((c) => {
    const path = c.hops.map((h) => `<span class="hop">${dot(h.status)}${esc(h.name)}:${h.port}</span>`);
    path.push(`<span class="hop mono">${esc(c.target_host)}:${c.target_port}</span>`);
    const first = c.hops[0];
    const entry = first ? `${first.addr}:${first.port}` : "";
    const entryLine = entry ? `<div class="entry"><span class="muted">入口</span><code>${esc(entry)}</code>${c.deleting
      ? "" : `<button class="small" data-action="copy" data-text="${esc(entry)}">复制</button>`}</div>` : "";
    let badge, actions;
    if (c.deleting) {
      badge = `<span class="badge removing"><i class="spinner"></i>删除中…</span>`;
      actions = `<button class="small danger" data-action="chain-delete" data-id="${c.id}" data-force="1">强制删除</button>`;
    } else {
      badge = c.pending.length
        ? `<span class="badge applying"><i class="spinner"></i>下发中…</span>`
        : `<span class="badge applied">✓ 已生效</span>`;
      actions = `<button class="small" data-action="chain-edit" data-id="${c.id}">编辑</button>
        <button class="small danger" data-action="chain-delete" data-id="${c.id}">删除</button>`;
    }
    return `
      <div class="card${c.deleting ? " dim" : ""}" data-key="chain-${c.id}">
        <div class="card-head">
          <div class="title">${esc(c.remark)}</div>
          <div class="chain-side">${badge}<div class="chain-actions">${actions}</div></div>
        </div>
        ${entryLine}
        <div class="path">${path.join(`<span class="arrow">→</span>`)}</div>
        ${pendingLine(c)}
      </div>`;
  });
  render(`
    ${listHead("转发", d.chains.length, "chains", "chain-new", "新增转发")}
    ${cards.join("") || emptyState("⇄", "还没有转发，点击右上角新增。")}`);
}

// ------------------------------------------------------------ 设置

async function settingsPage() {
  const s = await api("GET", "settings");
  const theme = localStorage.getItem("ictl-theme") || "auto";
  const opt = (v, label) => `<option value="${v}" ${theme === v ? "selected" : ""}>${label}</option>`;
  render(`
    <div class="page-head"><h1>设置</h1></div>
    <form class="panel" data-form="account">
      <h2>账号</h2>
      ${accountFields(s.username, false)}
      <div class="form-actions"><button class="primary">保存</button></div>
    </form>
    <div class="panel">
      <h2>外观</h2>
      <label><span>主题</span><select name="theme">${opt("auto", "跟随系统")}${opt("light", "浅色")}${opt("dark", "深色")}</select></label>
    </div>
    <form class="panel" data-form="settings">
      <h2>对外地址</h2>
      <label><span>用于生成节点的同步地址。留空时使用当前访问面板的地址（${esc(location.origin)}）。</span>
        <input name="public_url" value="${esc(s.public_url)}" placeholder="https://example.com"></label>
      <div class="form-error"></div>
      <div class="form-actions"><button class="primary">保存</button></div>
    </form>
    <div class="panel">
      <h2>信息</h2>
      <div class="kv-grid">
        <span>同步路径</span><span class="mono">${esc(s.sync_path)}</span>
        <span>面板路径</span><span class="mono">${esc(s.panel_path)}/</span>
        <span>版本</span><span>${esc(s.version)}</span>
      </div>
    </div>`);
}

// ------------------------------------------------------------ 弹窗

function openModal(html) {
  const m = $("#modal");
  m.innerHTML = `<div class="modal-box">${html}</div>`;
  m.hidden = false;
  m.querySelector("input, select")?.focus();
}

function closeModal() {
  const m = $("#modal");
  m.hidden = true;
  m.innerHTML = "";
}

function confirmBox(title, text, okLabel = "确定", danger = false) {
  return new Promise((resolve) => {
    openModal(`
      <h2>${esc(title)}</h2>
      <p>${text}</p>
      <div class="form-actions">
        <button type="button" data-confirm="0">取消</button>
        <button type="button" class="${danger ? "primary danger-bg" : "primary"}" data-confirm="1">${esc(okLabel)}</button>
      </div>`);
    $("#modal").querySelectorAll("[data-confirm]").forEach((b) =>
      b.addEventListener("click", () => {
        closeModal();
        resolve(b.dataset.confirm === "1");
      }));
  });
}

function keyModal(title, key, extra = "") {
  openModal(`
    <h2>${esc(title)}</h2>
    <p>节点需要在 <code>sudo ictl</code> 菜单 1 中填入新密钥，在此之前会显示认证失败，规则保持不变。</p>
    <div class="key-box"><code>${esc(key)}</code><button class="small" data-action="copy" data-text="${esc(key)}">复制</button></div>
    ${extra}
    <div class="form-actions"><button type="button" class="primary" data-action="modal-close">完成</button></div>`);
}

function nodeForm(node) {
  openModal(`
    <h2>${node ? "编辑节点" : "新增节点"}</h2>
    <form data-form="node" data-id="${node ? node.id : ""}">
      <label><span>名称</span><input name="name" value="${esc(node?.name)}" maxlength="64" required></label>
      <label><span>备注（选填，节点的说明）</span><textarea name="note" rows="3" maxlength="500">${esc(node?.note)}</textarea></label>
      <label><span>入站地址（IPv4 或域名，其他节点通过它连接到该节点）</span>
        <input name="addr" value="${esc(node?.addr)}" class="mono" required></label>
      <label><span>允许的入站端口（端口或范围，多个用逗号分隔，如 10000-20000,30000）</span>
        <input name="ports" value="${esc(node?.ports ?? "10000-65535")}" class="mono" required></label>
      ${node ? `<p class="hint">修改入站地址会重新生成密钥。</p>` : ""}
      <div class="form-error"></div>
      <div class="form-actions">
        <button type="button" data-action="modal-close">取消</button>
        <button class="primary">保存</button>
      </div>
    </form>`);
}

function hopRow(nodes, hop = {}) {
  const options = nodes.map((n) =>
    `<option value="${n.id}" ${hop.node_id === n.id ? "selected" : ""}>${esc(n.name)}（${esc(n.addr)} · 端口 ${esc(n.ports)}）</option>`);
  return `
    <div class="hop-row">
      <span class="hop-no"></span>
      <select name="hop-node"><option value="">选择节点</option>${options.join("")}</select>
      <input name="hop-port" value="${hop.port ?? ""}" placeholder="留空自动分配" inputmode="numeric">
      <button type="button" data-action="hop-up" title="上移">↑</button>
      <button type="button" data-action="hop-down" title="下移">↓</button>
      <button type="button" data-action="hop-del" title="删除">✕</button>
    </div>`;
}

function renumberHops() {
  document.querySelectorAll("#modal .hop-row").forEach((r, i) => ($(".hop-no", r).textContent = `${i + 1}`));
}

async function chainForm(chain) {
  const d = await api("GET", "nodes");
  const nodes = d.nodes.filter((n) => n.status !== "deleting");
  const hops = chain ? chain.hops : [{}];
  openModal(`
    <h2>${chain ? "编辑转发" : "新增转发"}</h2>
    <form data-form="chain" data-id="${chain ? chain.id : ""}">
      <label><span>别名</span><input name="remark" value="${esc(chain?.remark)}" maxlength="64" required></label>
      <div class="hops">
        <label style="margin-bottom:6px"><span>经过的节点（按顺序，每一跳填写该节点的入站端口，留空则自动分配一个未使用的端口）</span></label>
        <div id="hop-list">${hops.map((h) => hopRow(nodes, h)).join("")}</div>
        <button type="button" class="small" data-action="hop-add">添加一跳</button>
        <div class="field-error" data-error="hops"></div>
      </div>
      <div class="row">
        <label><span>目标地址（IPv4 或域名）</span><input name="target_host" value="${esc(chain?.target_host)}" class="mono" required></label>
        <label class="narrow"><span>目标端口</span><input name="target_port" value="${chain?.target_port ?? ""}" inputmode="numeric" required></label>
      </div>
      <div class="form-error"></div>
      <div class="form-actions">
        <button type="button" data-action="modal-close">取消</button>
        <button class="primary">保存</button>
      </div>
    </form>`);
  $("#modal form").hopTemplate = () => hopRow(nodes);
  renumberHops();
}

// ------------------------------------------------------------ 表单处理

function clearFormErrors(form) {
  form.querySelectorAll(".invalid").forEach((el) => el.classList.remove("invalid"));
  form.querySelectorAll("label > .field-error").forEach((el) => el.remove());
  form.querySelectorAll("[data-error]").forEach((el) => (el.textContent = ""));
  const fe = $(".form-error", form);
  if (fe) fe.textContent = "";
}

function showFormError(form, err) {
  const msg = err.message || String(err);
  const slot = err.field && form.querySelector(`[data-error="${err.field}"]`);
  const input = err.field && form.querySelector(`[name="${err.field}"]`);
  if (slot) {
    slot.textContent = msg;
  } else if (input) {
    input.classList.add("invalid");
    input.insertAdjacentHTML("afterend", `<div class="field-error">${esc(msg)}</div>`);
    input.focus();
  } else {
    const fe = $(".form-error", form);
    if (fe) fe.textContent = msg;
    else toast(msg, true);
  }
}

const FORMS = {
  async login(form, data) {
    await api("POST", "login", { username: data.username, password: data.password });
    me = null;   // 由 route() 重新获取，带上版本号
    if (location.hash === "") location.hash = "#/nodes";
    route();
  },

  async account(form, data) {
    if (data.password !== data.password2) throw Object.assign(new Error("两次输入的新密码不一致"), { field: "password2" });
    const r = await api("POST", "account", {
      current_password: data.current_password, username: data.username, password: data.password,
    });
    const first = me?.must_change;
    me = { ...me, username: r.username, must_change: false };
    toast("账号已更新");
    if (first) return route();
    form.reset();
    $("[name=username]", form).value = r.username;
  },

  async settings(form, data) {
    await api("PATCH", "settings", { public_url: data.public_url });
    toast("已保存");
    refresh();
  },

  async node(form, data) {
    const id = form.dataset.id;
    if (!id) {
      const r = await api("POST", "nodes", { name: data.name, note: data.note, addr: data.addr, ports: data.ports });
      closeModal();
      location.hash = `#/nodes/${r.id}`;
      return;
    }
    const r = await api("PATCH", `nodes/${id}`, { name: data.name, note: data.note, addr: data.addr, ports: data.ports });
    closeModal();
    if (r.key) {
      const ups = r.upstream.length ? `<p>以下节点的规则会自动更新：${r.upstream.map(esc).join("、")}</p>` : "";
      keyModal("入站地址已修改，密钥已重新生成", r.key, ups);
    } else {
      toast("已保存");
    }
    refresh();
  },

  async chain(form, data) {
    const id = form.dataset.id;
    const hops = [...form.querySelectorAll(".hop-row")].map((r) => ({
      node_id: Number($("[name=hop-node]", r).value) || null,
      port: $("[name=hop-port]", r).value.trim(),
    }));
    const body = { remark: data.remark, target_host: data.target_host, target_port: data.target_port, hops };
    const r = await api(id ? "PATCH" : "POST", id ? `chains/${id}` : "chains", body);
    closeModal();
    toast(r.assigned.length ? `已保存，自动分配端口：${r.assigned.join("、")}` : "已保存，正在下发到节点");
    refresh();
  },
};

document.addEventListener("submit", async (e) => {
  const form = e.target;
  const handler = FORMS[form.dataset.form];
  if (!handler) return;
  e.preventDefault();
  clearFormErrors(form);
  const button = form.querySelector("button.primary");
  if (button) button.disabled = true;
  try {
    await handler(form, Object.fromEntries(new FormData(form)));
  } catch (err) {
    if (err instanceof ApiError && (err.status === 401 && form.dataset.form !== "login")) return;
    if (err instanceof ApiError && err.status === 403 && err.data.must_change) return;
    showFormError(form, err);
  } finally {
    if (button && button.isConnected) button.disabled = false;
  }
});

// ------------------------------------------------------------ 按钮操作

const ACTIONS = {
  copy: (el) => copy(el.dataset.text),
  "modal-close": () => closeModal(),

  async logout() {
    await safe(() => api("POST", "logout"));
    me = null;
    showLogin();
  },

  "node-new": () => nodeForm(),
  "node-edit": () => nodeForm(currentNode),

  "key-toggle"() {
    const id = currentNode.id;
    revealedKeys.has(id) ? revealedKeys.delete(id) : revealedKeys.add(id);
    refresh();
  },

  async "node-rekey"() {
    const ok = await confirmBox("重置密钥", `重置后旧密钥立即失效，节点 <b>${esc(currentNode.name)}</b> 需要填入新密钥才能继续同步。`, "重置");
    if (!ok) return;
    const r = await api("POST", `nodes/${currentNode.id}/rekey`);
    keyModal("密钥已重置", r.key);
    refresh();
  },

  async "node-delete"(el) {
    const force = el.dataset.force === "1";
    const n = currentNode;
    const text = force
      ? `节点 <b>${esc(n.name)}</b> 尚未确认清空规则。强制删除会直接删除记录，节点上的规则不会被清理。`
      : n.status === "new"
        ? `确定删除节点 <b>${esc(n.name)}</b>？`
        : `将向节点 <b>${esc(n.name)}</b> 下发空配置，节点确认清空规则后自动删除。`;
    if (!(await confirmBox(force ? "强制删除节点" : "删除节点", text, "删除", true))) return;
    const r = await api("DELETE", `nodes/${n.id}${force ? "?force=1" : ""}`);
    if (r.deleted) {
      toast("已删除");
      location.hash = "#/nodes";
    } else {
      toast("已下发空配置，节点确认后自动删除");
      refresh();
    }
  },

  "sort-start"(el) {
    sortMode = el.dataset.kind;
    clearTimeout(refreshTimer);
    return refresh();
  },
  "sort-cancel"() {
    sortMode = null;
    refresh();
    schedule();
  },
  async "sort-save"(el) {
    const ids = [...app.querySelectorAll(".sort-card")].map((c) => Number(c.dataset.id));
    await api("POST", `${el.dataset.kind}/order`, { ids });
    sortMode = null;
    toast("顺序已保存");
    refresh();
    schedule();
  },

  "chain-new": () => chainForm(),
  "chain-edit": (el) => chainForm(chainsById[el.dataset.id]),

  async "chain-delete"(el) {
    const c = chainsById[el.dataset.id];
    const force = el.dataset.force === "1";
    const text = force
      ? `直接删除转发 <b>${esc(c.remark)}</b> 的记录。尚未确认的节点恢复连接后，会在同步时自动清除对应规则。`
      : `将通知相关节点移除转发 <b>${esc(c.remark)}</b> 的规则，所有节点确认后自动删除。`;
    if (!(await confirmBox(force ? "强制删除转发" : "删除转发", text, "删除", true))) return;
    const r = await api("DELETE", `chains/${c.id}${force ? "?force=1" : ""}`);
    toast(r.deleted ? "已删除" : "已通知节点，确认后自动删除");
    refresh();
  },

  "hop-add"(el) {
    const form = el.closest("form");
    $("#hop-list").insertAdjacentHTML("beforeend", form.hopTemplate());
    renumberHops();
  },
  "hop-up"(el) {
    const row = el.closest(".hop-row");
    if (row.previousElementSibling) row.parentNode.insertBefore(row, row.previousElementSibling);
    renumberHops();
  },
  "hop-down"(el) {
    const row = el.closest(".hop-row");
    if (row.nextElementSibling) row.parentNode.insertBefore(row.nextElementSibling, row);
    renumberHops();
  },
  "hop-del"(el) {
    if (document.querySelectorAll("#modal .hop-row").length > 1) el.closest(".hop-row").remove();
    renumberHops();
  },
};

document.addEventListener("click", (e) => {
  if (e.target.id === "modal") return closeModal();
  const el = e.target.closest("[data-action]");
  if (!el || !ACTIONS[el.dataset.action]) return;
  e.preventDefault();
  safe(() => ACTIONS[el.dataset.action](el));
});

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !$("#modal").hidden) closeModal();
});

// ------------------------------------------------------------ 拖动排序
// 只能从手柄开始拖动（触屏上其余区域仍可正常滚动）；拖动时其他卡片平滑让位

let drag = null;

document.addEventListener("pointerdown", (e) => {
  const handle = e.target.closest(".drag-handle");
  if (!handle || e.button > 0) return;
  e.preventDefault();
  const card = handle.closest(".sort-card");
  handle.setPointerCapture(e.pointerId);
  drag = { card, offset: e.clientY - card.getBoundingClientRect().top };
  card.classList.add("dragging");
});

document.addEventListener("pointermove", (e) => {
  if (!drag) return;
  const { card } = drag;
  const list = card.parentNode;
  const y = e.clientY;
  if (y < 60) scrollBy(0, -12);
  else if (y > innerHeight - 60) scrollBy(0, 12);

  const others = [...list.children].filter((c) => c !== card);
  const before = others.find((c) => {
    const r = c.getBoundingClientRect();
    return y < r.top + r.height / 2;
  }) || null;
  if (card.nextElementSibling !== before) {
    const tops = new Map(others.map((c) => [c, c.getBoundingClientRect().top]));
    list.insertBefore(card, before);
    for (const [c, top] of tops) {
      const dy = top - c.getBoundingClientRect().top;
      if (!dy) continue;
      c.style.transition = "none";
      c.style.transform = `translateY(${dy}px)`;
      void c.offsetWidth;
      c.style.transition = "";
      c.style.transform = "";
    }
  }
  card.style.transform = "";
  card.style.transform = `translateY(${y - drag.offset - card.getBoundingClientRect().top}px)`;
});

function endDrag() {
  if (!drag) return;
  drag.card.style.transform = "";
  drag.card.classList.remove("dragging");
  drag = null;
}
document.addEventListener("pointerup", endDrag);
document.addEventListener("pointercancel", endDrag);

// 记住折叠区的展开状态（toggle 事件不冒泡，用捕获）
document.addEventListener("toggle", (e) => {
  const key = e.target.dataset?.key;
  if (!key) return;
  e.target.open ? openDetails.add(key) : openDetails.delete(key);
}, true);

document.addEventListener("change", (e) => {
  if (e.target.name !== "theme") return;
  localStorage.setItem("ictl-theme", e.target.value);
  document.documentElement.dataset.theme = e.target.value;
});

// ------------------------------------------------------------ 启动

if (location.protocol === "http:" && !["localhost", "127.0.0.1", "[::1]"].includes(location.hostname)) {
  $("#http-warning").hidden = false;
}
document.querySelectorAll(".logo").forEach((el) => (el.innerHTML = LOGO));
window.addEventListener("hashchange", route);
route();
