"use strict";

// ------------------------------------------------------------ 常量与工具

const STATUS = {
  online: ["在线", "green"],
  new: ["未连接", "grey"],
  offline: ["离线", "red"],
  skew: ["时间不一致", "yellow"],
  syncing: ["等待中", "blue"],
  synced: ["已同步", "green"],
  error: ["异常", "yellow"],
  deleting: ["删除中", "grey"],
};
// 节点端版本与服务端不一致时的说明
const AGENT_UPDATE = {
  pending: "等待节点自动更新",
  disabled: "节点已关闭自动更新，可在节点上运行 <code>sudo ictl --update</code> 更新",
  legacy: "该版本节点端每 6 小时自行检查更新，也可在节点上运行 <code>sudo ictl --update</code> 立即更新",
  unavailable: "服务端缺少同版本的节点端脚本，无法自动更新，可在节点上运行 <code>sudo ictl --update</code> 更新",
  newer: "节点端版本高于目标版本",
};
const LEVEL = { white: "无手动同步", ok: "正常", fault: "故障", lost: "失联", none: "无数据" };
const LIVE = new Set(["syncing", "deleting"]);   // 带呼吸动画的状态
const REFRESH_MS = 5000;
const LOGO = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M4 8h14l-3.5-3.5M20 16H6l3.5 3.5"/></svg>`;

const $ = (sel, root = document) => root.querySelector(sel);
const app = $("#app");

let me = null;                    // {username, must_change}
let currentView = null;           // 当前页面的渲染函数
let pageVersion = 0;              // 页面切换后使待完成的导航、弹窗请求失效
let viewRequest = 0;              // 同一页面只接受最新一次刷新
let refreshTimer = null;
let currentNode = null;           // 详情页的节点
let list = null;                  // 当前列表页：{kind, data, draw}，搜索和筛选时用已有数据重绘
let sortMode = null;              // 正在排序的列表："nodes" / "chains"
let sortInitial = null, sortBusy = false;
const sortValues = () => JSON.stringify(['.sort-group','.sort-card'].map(selector => [...app.querySelectorAll(selector)].map(el => el.dataset.id)));
const sortChanged = () => sortMode && sortInitial !== null && sortValues() !== sortInitial;
const openDetails = new Set();    // 已展开的折叠区，刷新后保持
const revealedKeys = new Set();   // 已显示密钥的节点
const filters = { nodes: {}, routes: {}, targets: {}, chains: {}, links: {}, configs: {} };   // 搜索和筛选条件，刷新页面后清空
const collapsed = new Set(JSON.parse(localStorage.getItem("ictl-collapsed") || "[]"));   // 已折叠的分组

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

function moreActions(key, actions) {
  return `<details class="more-actions" data-key="${key}" ${openDetails.has(key)?'open':''}><summary aria-label="更多操作">更多</summary><div class="more-menu">${actions}</div></details>`;
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
    return `<span class="lv-${lv}" title="${clock(t)} – ${clock(t + b.window)}  ${b.counts ? (b.counts[i] ? `手动同步 ${b.counts[i]} 次，最近 ${clock(b.times[i])}；结果见事件记录` : "无手动同步") : LEVEL[lv]}"></span>`;
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
  let copied = false;
  try {
    await navigator.clipboard.writeText(text);
    copied = true;
  } catch {
    // HTTP 页面没有剪贴板 API，退回旧方法
    const t = document.createElement("textarea");
    t.value = text;
    t.style.position = "fixed";
    t.style.opacity = "0";
    const focused = document.activeElement;
    try {
      document.body.append(t);
      t.select();
      copied = document.execCommand("copy");
    } catch {} finally {
      t.remove();
      if (focused?.isConnected) focused.focus({preventScroll:true});
    }
  }
  toast(copied ? "已复制" : "复制失败，请手动选择内容复制", !copied);
  return copied;
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
  const request = viewRequest;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), path.endsWith('/query') ? 45000 : 20000);
  const opt = { method, headers: {}, credentials: "same-origin", signal: controller.signal };
  if (method !== "GET") {
    opt.headers["Content-Type"] = "application/json";
    opt.body = JSON.stringify(body || {});
  }
  let res, data = {};
  try {
    res = await fetch("api/" + path, opt);
    try { data = await res.json(); }
    catch (err) { if (controller.signal.aborted || res.ok) throw err; }
  } catch (err) {
    if (controller.signal.aborted) throw new ApiError(0, {error:"请求超时，请刷新核对操作结果后再重试"});
    if (res) throw new ApiError(0, {error:"服务端响应异常，请刷新核对操作结果"});
    throw new ApiError(0, { error: "无法连接服务端" });
  } finally {
    clearTimeout(timer);
  }
  if (res.ok) return data;
  if (request === viewRequest && res.status === 401 && path !== "login") {
    me = null;
    showLogin();
  } else if (request === viewRequest && res.status === 403 && data.must_change) {
    me = { ...me, must_change: true };
    showChangeAccount();
  }
  throw new ApiError(res.status, data);
}

// 执行异步操作，统一处理错误
async function safe(fn) {
  const request = viewRequest;
  try {
    return await fn();
  } catch (e) {
    if (!(e instanceof ApiError)) throw e;
    if (request !== viewRequest) return;
    if (e.status === 401 || (e.status === 403 && e.data.must_change)) return;
    toast(e.message, true);
  }
}

// ------------------------------------------------------------ 页面切换与刷新

function leaveView() {
  if (typeof releaseTopology === "function") releaseTopology();
  releaseQuality();
  pageVersion++;
  viewRequest++;
  currentView = null;
  currentNode = null;
  list = null;
  sortMode = null;
  sortInitial = null;
  clearTimeout(refreshTimer);
}

async function show(view, auto) {
  const page = pageVersion;
  currentView = view;
  list = null;
  view.auto = auto;
  fullRender = true;
  await refresh();
  if (page === pageVersion) schedule();
}

function schedule() {
  clearTimeout(refreshTimer);
  if (!currentView?.auto || sortMode) return;
  const page = pageVersion;
  refreshTimer = setTimeout(async () => {
    if ($("#modal").hidden && !document.hidden && !sortMode) await refresh();
    if (page === pageVersion) schedule();
  }, currentView.refreshMS || REFRESH_MS);
}

function refresh() {
  if (!currentView) return;
  const request = ++viewRequest;
  return safe(() => currentView(request));
}

let renderedHash = location.hash;
async function route() {
  if (sortBusy) {
    history.replaceState(null, '', location.pathname + location.search + renderedHash);
    return;
  }
  if (sortChanged() && !(await confirmBox("放弃未保存的排序？", "离开后将丢弃此次顺序调整。", "放弃", true))) {
    history.replaceState(null, '', location.pathname + location.search + renderedHash);
    return;
  }
  if ((modalChanged() || modalState?.busy) && !(await requestCloseModal())) {
    history.replaceState(null, '', location.pathname + location.search + renderedHash);
    return;
  }
  renderedHash = location.hash;
  leaveView();
  const version = pageVersion;
  closeModal();
  if (!me) {
    try {
      const user = await api("GET", "me");
      if (version !== pageVersion) return;
      me = user;
    } catch {
      return;
    }
  }
  if (me.must_change) return showChangeAccount();

  document.body.classList.remove("auth-page");
  footer(true);
  $("#nav").hidden = false;
  const [page, id, detailId] = (location.hash || "#/nodes").replace(/^#\/?/, "").split("/");
  document.querySelectorAll("[data-nav]").forEach((a) => a.classList.toggle("active", a.dataset.nav === page));

  if (page === "quality" && ["routes", "configs", "links"].includes(id) && /^\d+$/.test(detailId || "")) {
    const view = (request) => qualityPage(id, Number(detailId), request);
    view.refreshMS = 10000;
    return show(view, true);
  }
  if (page === "nodes" && id) return show((request) => nodeDetail(Number(id), request), true);
  if (page === "nodes") return show(nodeList, true);
  if (page === "configs" || page === "chains") return show(configSummary, true);
  if (page === "routes" && id) return show((request) => linkDetail(Number(id), request), true);
  if (page === "routes") return show(linkList, true);
  if (page === "pools") return show(poolList, true);
  if (page === "targets") return show(targetList, true);
  if (page === "subscriptions") return show(subscriptionList, true);
  if (page === "settings") return show(settingsPage, false);
  location.hash = "#/nodes";
}

// ------------------------------------------------------------ 登录与账号

function showLogin() {
  leaveView();
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
  leaveView();
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

const KIND_LABEL = { nodes: "节点", routes: "链路", targets: "目标", chains: "配置", links: "链路", configs: "配置" };

// 列表页标题栏：排序模式下显示「取消 / 完成」；搜索或筛选时不能排序
function listHead(kind, total, shown, newAction, newLabel, extraActions = "") {
  const filtering = shown !== total;
  const buttons = sortMode === kind
    ? `<button data-action="sort-cancel">取消</button>
       <button class="primary" data-action="sort-save" data-kind="${kind}">完成</button>`
    : `${total > 1 || list.data.groups.length > 1 ? `<button data-action="sort-start" data-kind="${kind}" ${isFiltering(kind)
        ? `disabled title="搜索或筛选时不能排序"` : ""}>排序</button>` : ""}
       ${extraActions}
       <button class="primary" data-action="${newAction}">${newLabel}</button>
       ${moreActions(`list-actions-${kind}`, `<button data-action="group-new" data-kind="${kind}">新建分组</button>`)}`;
  return `<div class="page-head"><h1>${KIND_LABEL[kind]}<span class="count">${filtering ? `${shown} / ${total}` : total || ""}</span></h1>
    <div class="head-actions">${buttons}</div></div>`;
}

function isFiltering(kind) {
  return Object.values(filters[kind]).some((v) => v && String(v).trim());
}

function saveCollapsed() {
  localStorage.setItem("ictl-collapsed", JSON.stringify([...collapsed]));
}

// 按分组显示：items 已按分组和组内顺序排列。只有默认分组时不显示组头。
// 搜索或筛选时隐藏没有匹配项的分组，有匹配项的分组临时展开
function groupedList(kind, items, card, summary, editable = true) {
  const groups = list.data.groups;
  const rows = kind === 'nodes' || kind === 'targets';
  if (groups.length === 1) return rows ? `<div class="resource-rows">${items.map(card).join('')}</div>` : items.map(card).join("");
  const filtering = isFiltering(kind);
  return groups.map((g) => {
    const members = items.filter((x) => x.group_id === g.id);
    if (filtering && !members.length) return "";
    const key = `${kind}-${g.id}`;
    return `<section class="group${!filtering && collapsed.has(key) ? " closed" : ""}" data-key="group-${key}">
      ${groupHead(kind, g, `${members.length} 个${summary(members)}`, "", editable)}
      <div class="group-body${rows?' resource-rows':''}">${members.map(card).join("") || `<div class="group-empty">空分组</div>`}</div>
    </section>`;
  }).join("");
}

function groupHead(kind, g, sum, handle = "", editable = true) {
  return `<div class="group-head" data-action="group-toggle" data-group="${kind}-${g.id}">
    ${handle}<span class="chevron"></span><span class="group-name">${esc(g.name)}</span><span class="group-sum">${sum}</span>
    ${handle || !editable ? "" : `<span class="group-actions">
      <button class="small" data-action="group-rename" data-id="${g.id}">改名</button>
      ${g.builtin ? "" : `<button class="small danger" data-action="group-delete" data-id="${g.id}">删除</button>`}
    </span>`}
  </div>`;
}

// 排序模式：精简的卡片，左侧为拖动手柄；有多个分组时，分组标题也可以拖动
function sortList(kind, items, inner) {
  const card = (x) => `
    <div class="card sort-card sort-item" data-key="${kind}-${x.id}" data-id="${x.id}">
      <span class="drag-handle" title="拖动排序">⠿</span><div class="sort-body">${inner(x)}</div>
    </div>`;
  const groups = list.data.groups;
  if (groups.length === 1) {
    return `<p class="hint sort-hint">按住左侧手柄拖动调整顺序，点「完成」保存。</p>
      <div class="sort-list">${items.map(card).join("")}</div>`;
  }
  return `<p class="hint sort-hint">拖动分组标题左侧的手柄调整分组顺序，拖动卡片左侧的手柄调整组内顺序，点「完成」保存。更换分组请在编辑中选择。</p>
    <div class="sort-list">${groups.map((g) => {
      const members = items.filter((x) => x.group_id === g.id);
      const key = `${kind}-${g.id}`;
      return `<section class="group sort-group sort-item${collapsed.has(key) ? " closed" : ""}" data-key="sgroup-${key}" data-id="${g.id}">
        ${groupHead(kind, g, `${members.length} 个`, `<span class="drag-handle" title="拖动排序">⠿</span>`)}
        <div class="group-body">${members.map(card).join("") || `<div class="group-empty">空分组</div>`}</div>
      </section>`;
    }).join("")}</div>`;
}

// 搜索与筛选栏；控件的值在渲染后由 syncFilters 同步
function filterBar(kind, controls) {
  return `<div class="filters" data-filters="${kind}">${controls.join("")}
    ${isFiltering(kind) ? `<button class="small link" data-action="filter-clear" data-kind="${kind}">清除</button>` : ""}</div>`;
}

function filterSelect(name, label, options) {
  return `<select data-filter="${name}"><option value="">${label}</option>${options}</select>`;
}

function filterInput(name, placeholder, type = "text") {
  return `<input type="${type}" data-filter="${name}" placeholder="${placeholder}" autocomplete="off">`;
}

function plainOptions(pairs) {
  return pairs.map(([v, label]) => `<option value="${esc(v)}">${esc(label)}</option>`).join("");
}

function groupOptions(groups) {
  return groups.length > 1 ? plainOptions(groups.map((g) => [g.id, g.name])) : "";
}

// 节点下拉选项：先按分组、再按组内顺序；有多个分组时用分组标题分隔
function nodeOptions(nodes, groups, selected, text = (n) => esc(n.name)) {
  const opt = (n) => `<option value="${n.id}" ${n.id === selected ? "selected" : ""}>${text(n)}</option>`;
  if (groups.length < 2) return nodes.map(opt).join("");
  return groups.map((g) => {
    const members = nodes.filter((n) => n.group_id === g.id);
    return members.length ? `<optgroup label="${esc(g.name)}">${members.map(opt).join("")}</optgroup>` : "";
  }).join("");
}

function syncFilters() {
  app.querySelectorAll("[data-filter]").forEach((el) => {
    const v = filters[el.closest("[data-filters]").dataset.filters][el.dataset.filter] || "";
    if (el.value !== v) el.value = v;
  });
}

const has = (text, q) => String(text).toLowerCase().includes(q.trim().toLowerCase());

const countText = (n, label, cls) => (n ? `<span class="${cls}"> · ${n} ${label}</span>` : "");

async function nodeList(request) {
  const data = await api("GET", "nodes");
  if (request !== viewRequest) return;
  const kind = "nodes";
  data[kind] = data.nodes;
  list = { kind, data, draw: drawNodes };
  drawNodes();
}

function drawNodes() {
  const d = list.data, kind = list.kind;
  if (sortMode === kind) {
    return render(listHead(kind, d.nodes.length, d.nodes.length) +
      sortList(kind, d.nodes, (n) => `<div class="title">${esc(n.name)}</div>${nodeSub(n)}`));
  }
  const f = filters[kind];
  const items = d.nodes.filter((n) =>
    (!f.mode || n.desired_mode === f.mode) && (!f.q || has(n.name, f.q)) && (!f.status || n.status === f.status) && (!f.group || n.group_id === Number(f.group)));
  const card = (n) => `
    <a class="card node-card" href="#/nodes/${n.id}" data-key="node-${n.id}">
      <div class="card-head">
        <div class="node-head"><div class="title" title="${esc(n.name)}"><span class="mode-tag ${n.desired_mode}">${n.desired_mode === "passive" ? "被动" : "主动"}</span>${esc(n.name)}</div>${nodeSub(n)}</div>
        <div class="meta">${pill(n.status)}<span class="muted">${ago(n.last_seen, d.now)}</span></div>
      </div>
      ${bar(n.bar)}
      ${n.wake?.error ? `<p class="hint t-yellow">${esc(n.wake.error)}</p>` : ""}
    </a>`;
  const summary = (ns) =>
    countText(ns.filter((n) => n.status === "offline").length, "离线", "t-red") +
    countText(ns.filter((n) => ["error", "skew"].includes(n.status)).length, "异常", "t-yellow");
  const tools = d.nodes.length ? filterBar(kind, [
    filterInput("q", "搜索名称", "search"),
    filterSelect("mode", "全部模式", plainOptions([["active", "主动"], ["passive", "被动"]])),
    filterSelect("status", "全部状态", plainOptions(Object.entries(STATUS).map(([k, [label]]) => [k, label]))),
    d.groups.length > 1 ? filterSelect("group", "全部分组", groupOptions(d.groups)) : "",
  ]) : "";
  let body = groupedList(kind, items, card, summary);
  if (!items.length && isFiltering(kind)) body = emptyState("⌕", "没有匹配的节点。");
  else if (!d.nodes.length && d.groups.length === 1) body = emptyState("◎", "还没有节点，点击右上角新增。");
  render(listHead(kind, d.nodes.length, items.length, "node-new", "新增节点") + tools + body);
  syncFilters();
}

// ------------------------------------------------------------ 节点详情

function detailsBlock(key, title, content) {
  return `<details data-key="${key}" ${openDetails.has(key) ? "open" : ""}><summary>${title}</summary>${content}</details>`;
}

async function nodeDetail(id, request) {
  let d;
  try {
    d = await api("GET", `nodes/${id}`);
  } catch (e) {
    if (request !== viewRequest) return;
    if (e instanceof ApiError && e.status === 404) {
      location.hash = "#/nodes";
      return;
    }
    throw e;
  }
  if (request !== viewRequest) return;
  const n = (currentNode = d.node);
  const url = syncAddress(d.public_url, d.sync_path);
  const shown = revealedKeys.has(id);
  const quote = (value) => "'" + String(value).replaceAll("'", "'\\''") + "'";
  const install = (key) => `(f=$(mktemp) && trap 'rm -f "$f"' EXIT && curl -fsSL ${quote(d.installer_url || d.agent_url)} -o "$f" && sudo bash "$f" --url ${quote(url)} --key ${quote(key)}${d.installer_url ? ` --kind ${quote(n.desired_mode)}` : ""})`;

  const rules = d.rules.length
    ? `<table><thead><tr><th>入站端口</th><th>转发到</th><th>所属转发</th></tr></thead><tbody>${d.rules.map((r) => `
        <tr><td class="mono">${r.port}</td><td class="mono">${esc(r.host)}:${r.tport}</td>
        <td><a href="#/configs">${esc(r.chain_remark)}</a></td></tr>`).join("")}</tbody></table>`
    : `<div class="muted">${n.status === "deleting" ? "正在删除，已下发空配置" : "没有转发规则"}</div>`;

  const skew = n.skew == null ? "-" : `${n.skew > 0 ? "+" : ""}${n.skew} 秒`;
  const more = `<div class="kv-grid">
    <span>分组</span><span>${esc(n.group)}</span>
    <span>来源 IP</span><span class="mono">${esc(n.src_ip || "-")}</span>
    <span>时差</span><span>${skew}</span>
    <span>节点端版本</span><span>${esc(n.agent || "-")}${AGENT_UPDATE[n.agent_update]
      ? `<span class="muted">（目标转发 Agent 为 ${esc(d.forward_version)}，${(n.desired_mode === "passive" && n.agent_update === "pending") ? "等待本机手动同步时更新" : AGENT_UPDATE[n.agent_update]}）</span>` : ""}</span>
    <span>已安装组件</span><span>${esc(n.installed_agent || "legacy")} / ${esc(n.backend || "iptables")}</span>
    <span>管理连接</span><span>${esc(n.connection_state || "旧版轮询")}</span>
    <span>确认进度</span><span>${n.applied_generation} / ${n.desired_generation}</span>
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
      <div class="detail-actions"><div class="meta"><span class="muted">${n.desired_mode === "passive" ? "被动" : "主动"}</span>${pill(n.status)}<span class="muted">最后上报 ${ago(n.last_seen, d.now)}</span></div><button data-action="node-edit">编辑</button>${moreActions(`node-actions-${id}`, `<button data-action="node-rekey">重置密钥</button>${n.status === 'deleting'?'<button class="danger" data-action="node-delete" data-force="1">强制删除</button>':'<button class="danger" data-action="node-delete">删除</button>'}`)}</div>
    </div>
    ${n.note ? `<div class="panel note-full"><h2>备注</h2><div>${esc(n.note)}</div></div>` : ""}
    <div class="detail-grid"><div class="panel">${bar(n.bar)}
      ${n.installed_agent === "legacy" || !n.installed_agent ? `<p class="t-yellow">此节点尚未迁移。请在节点执行下方一键接入命令；新版主动 Agent 确认安装后才能加入池并显示采集信息。</p>` : ""}
      ${n.desired_mode === "passive" || n.manual_required ? `<p class="hint">本机运行 <code>sudo ictl --sync</code> 或菜单「同步更新」。等待状态会持续到当前变更确认成功。白条上的绿色表示手动同步活动或远程同步成功，执行结果见下方事件。</p>` : ""}
      ${n.desired_mode === "passive" ? `<p class="hint">已同步表示最近一次确认结果，不表示实时在线；被动模式不自动轮询。</p>` : ""}
      ${n.desired_mode === "passive" ? `<p class="hint">远程唤醒：${n.wake_enabled ? `已开启，UDP ${n.wake_port}。${n.wake?.state === 'waiting' ? '正在等待同步确认，超过 30 秒转为等待手动同步。' : n.wake?.state === 'confirmed' ? '最近一次同步已确认。' : '请确保节点本地已配置白名单和相同端口。'}` : '未开启，配置变更需要本机手动同步。'}</p>` : ""}
      ${n.wake?.error ? `<p class="t-yellow">${esc(n.wake.error)}</p>` : ""}
      ${n.err ? `<p class="t-yellow">最近错误：${esc(n.err)}</p>` : ""}
    </div>
    <div class="panel">
      <h2>接入信息</h2>
      <div class="kv"><span>同步地址</span><code>${esc(url)}</code>
        <button class="small" data-action="copy" data-text="${esc(url)}">复制</button></div>
      <div class="kv"><span>密钥</span><code>${shown ? esc(n.key) : "•".repeat(32)}</code>
        <button class="small" data-action="key-toggle">${shown ? "隐藏" : "显示"}</button>
        <button class="small" data-action="copy" data-text="${esc(n.key)}">复制</button></div>
      <div class="kv"><span>一键接入</span><code>${esc(install(shown ? n.key : "••••••"))}</code>
        <button class="small" data-action="copy" data-text="${esc(install(n.key))}">复制</button></div>
      <p class="hint">在对应节点执行一键接入命令。旧节点升级或切换类型也使用此入口，保留节点身份；新主动 Agent 需要 HTTPS。</p>
    </div>
    </div><div class="panel"><h2>转发规则<span class="count">允许端口 ${esc(n.ports)}</span></h2><div class="table-scroll">${rules}</div></div>
    ${telemetryPanel(d)}
    ${detailsBlock(`more-${id}`, "更多信息", more)}
    ${detailsBlock(`events-${id}`, "事件记录", events)}`);
}

function bytesText(value) {
  if (value == null || value === "") return "-";
  try {
    let n = BigInt(value || 0), unit = 1n;
    for (const suffix of ["B", "KiB", "MiB", "GiB", "TiB", "PiB"]) {
      if (n < unit * 1024n || suffix === "PiB") return `${n * 100n / unit / 100n}.${String(n * 100n / unit % 100n).padStart(2, "0")} ${suffix}`;
      unit *= 1024n;
    }
  } catch (_) { return "-"; }
}

function telemetryPanel(d) {
  const n = d.node, t = d.telemetry || {}, m = t.metrics || {}, inv = t.inventory || {};
  if (n.installed_agent !== "active" && !(t.traffic || []).length) return "";
  const names = new Map(d.rules.map(r => [r.id, r.chain_remark]));
  const traffic = (t.traffic || []).map(r => `<tr><td>${esc(names.get(r.rule_id) || `历史规则 ${r.rule_id}`)}</td><td>${esc(r.protocol)}</td><td title="${esc(r.up)} 字节">${bytesText(r.up)}</td><td title="${esc(r.down)} 字节">${bytesText(r.down)}</td><td>${esc(r.quality)}</td></tr>`).join("");
  const mem = m.memory || {};
  const net = Object.entries(m.interfaces || {}).map(([name,v]) => `<tr><td>${esc(name)}</td><td>${bytesText(v.rx_bytes)}</td><td>${bytesText(v.tx_bytes)}</td><td>${v.rx_bytes_per_second == null ? "-" : bytesText(Math.round(v.rx_bytes_per_second)) + "/s"}</td><td>${v.tx_bytes_per_second == null ? "-" : bytesText(Math.round(v.tx_bytes_per_second)) + "/s"}</td></tr>`).join("");
  const ports = ((t.ports || {}).rows || []).slice(0,100).map(p => `<tr><td>${esc(p.source)}</td><td>${esc(p.protocol)}</td><td>${esc(p.port)}</td><td>${esc(p.address || p.target || "-")}</td></tr>`).join("");
  const queries = [["inventory","系统信息"],["ports","端口详情"],["network","网络信息"],["rules","当前规则"],["logs","最近日志"]];
  const history = (t.history || []).slice(-20).map(r => `<tr><td>${fmtTime(r.time)}</td><td>${r.data.values?.cpu_percent ? Number(r.data.values.cpu_percent.mean).toFixed(1) + "%" : "-"}</td><td>${r.data.values?.memory_used_percent ? Number(r.data.values.memory_used_percent.mean).toFixed(1) + "%" : "-"}</td><td>${r.data.samples}</td></tr>`).join("");
  const buttons = n.installed_agent === "active" && n.desired_mode === "active" ? queries.map(([kind,label]) => `<button class="small" data-action="node-query" data-kind="${kind}">${label}</button>`).join("") : "";
  const diagnostics = n.installed_agent === "active" && n.desired_mode === "active" ? d.rules.map(r => `<button class="small" data-action="node-query" data-kind="diagnose" data-rule="${r.id}">诊断 ${esc(r.chain_remark)}</button><button class="small" data-action="node-query" data-kind="connections" data-rule="${r.id}">连接 ${esc(r.chain_remark)}</button>`).join("") : "";
  return `<div class="panel"><h2>主动 Agent 采集</h2><p class="hint">最近采样 ${fmtTime(t.metrics_at)}；离线或切为被动后保留历史值，不代表实时状态。</p>
    <div class="kv-grid"><span>内核 / 架构</span><span>${esc(inv.kernel || "-")} / ${esc(inv.arch || "-")}</span>
    <span>CPU</span><span>${m.cpu_percent == null ? "-" : Number(m.cpu_percent).toFixed(1) + "%"}</span>
    <span>内存可用 / 总量</span><span>${bytesText(mem.MemAvailable)} / ${bytesText(mem.MemTotal)}</span>
    <span>磁盘可用 / 总量</span><span>${bytesText(m.disk?.available)} / ${bytesText(m.disk?.total)}</span>
    <span>连接跟踪</span><span>${esc(m.conntrack_count || "-")} / ${esc(m.conntrack_max || "-")}</span></div>
    ${m.traffic_quality ? `<p class="t-yellow">${esc(m.traffic_quality)}</p>` : ""}
    <h3>转发累计流量</h3><p class="hint">按本节点每条规则的 IPv4 转发字节计数，包含协议头及重传；多跳分别计数。上行为入口到目标，下行为目标返回。主机网卡流量见下表。</p>
    <table><thead><tr><th>规则</th><th>协议</th><th>上行</th><th>下行</th><th>计数状态</th></tr></thead><tbody>${traffic || '<tr><td colspan="5">等待采集</td></tr>'}</tbody></table>
    ${detailsBlock(`network-${n.id}`, "网卡与端口", `<table><thead><tr><th>网卡</th><th>接收</th><th>发送</th><th>接收速率</th><th>发送速率</th></tr></thead><tbody>${net}</tbody></table><p>端口快照（最多显示 100 条）</p><table><thead><tr><th>来源</th><th>协议</th><th>端口</th><th>地址</th></tr></thead><tbody>${ports}</tbody></table>`)}
    ${detailsBlock(`history-${n.id}`, "最近 20 分钟（采样均值）", `<table><thead><tr><th>时间</th><th>CPU</th><th>内存使用率</th><th>样本数</th></tr></thead><tbody>${history}</tbody></table><p class="hint">缺失的时间段没有采样，不按零处理。</p>`)}
    <div class="actions">${buttons}${diagnostics}</div></div>`;
}

// ------------------------------------------------------------ 转发

// 等待确认的节点，附带离线、异常等状态
function pendingLine(c) {
  if (!c.pending.length) return "";
  const who = c.pending.map((p) => {
    const note = ["synced", "syncing"].includes(p.status) ? "" : `<span class="muted">（${STATUS[p.status]?.[0] || p.status}）</span>`;
    const passiveNote = p.wake_enabled && p.wake?.state === 'waiting' ? '等待自动同步' : '需本机同步';
    return `<span class="who" title="${esc(p.wake?.error || p.err || '')}">${dot(p.status)}${esc(p.name)}${note}${p.desired_mode === "passive" ? `<span class="muted">（${passiveNote}）</span>` : ""}</span>`;
  });
  return `<div class="pending">等待确认：${who.join("")}</div>`;
}

function chainStatus(c) {
  return c.status || (c.deleting ? "deleting" : c.pending.length ? "applying" : "applied");
}

async function chainList(request) {
  const data = await api("GET", "configs");
  if (request !== viewRequest) return;
  list = { kind: "chains", data, draw: drawChains };
  drawChains();
}

function drawChains() {
  const d = list.data;
  if (sortMode === "chains") {
    return render(listHead("chains", d.chains.length, d.chains.length) + sortList("chains", d.chains, (c) =>
      `<div class="title">${esc(c.remark)}</div>
       <div class="sub">${c.hops.length ? c.hops.map((h) => esc(h.name)).join(" → ") + " → " : "直连目标 · "}<span class="mono">${esc(c.target_host)}:${c.target_port}</span></div>`));
  }
  const f = filters.chains;
  const items = d.chains.filter((c) =>
    (!f.q || has(c.remark, f.q)) &&
    (!f.entry || c.hops[0]?.node_id === Number(f.entry)) &&
    (!f.via || c.hops.some((h) => h.node_id === Number(f.via))) &&
    (!f.target || has(`${c.target_host}:${c.target_port}`, f.target)) &&
    (!f.status || chainStatus(c) === f.status) &&
    (!f.group || c.group_id === Number(f.group)));
  const card = (c) => {
    const path = c.hops.map((h) => `<span class="hop">${dot(h.status)}${esc(h.name)}:${h.port}</span>`);
    path.push(`<span class="hop mono">${esc(c.target_host)}:${c.target_port}</span>`);
    const first = c.hops[0];
    const entry = c.connection ? `${c.connection.host}:${c.connection.port}` : first ? `${first.addr}:${first.port}` : "";
    const entryLine = entry ? `<div class="entry"><span class="muted">入口</span><code>${esc(entry)}</code>${c.deleting
      ? "" : `<button class="small" data-action="copy" data-text="${esc(entry)}">复制</button>`}</div>` : "";
    let badge, actions;
    if (c.deleting) {
      badge = `<span class="badge removing"><i class="spinner"></i>删除中…</span>`;
      actions = `<button class="small danger" data-action="chain-delete" data-id="${c.id}" data-force="1">强制删除</button>`;
    } else {
      badge = c.status === "saved" ? `<span class="badge">已保存</span>`
        : c.status === "error" ? `<span class="badge removing">异常</span>` : c.pending.length
        ? `<span class="badge applying"><i class="spinner"></i>等待中…</span>`
        : `<span class="badge applied">✓ 已应用</span>`;
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
        <div class="sub">${c.route_id ? `链路：${esc(c.route_name)}` : "直连目标"} · 目标：${esc(c.target_name)}</div>
        ${c.connection && c.connection.protocol !== "endpoint" ? `<div class="entry"><span class="muted">连接信息</span><button class="small" data-action="connection" data-id="${c.id}">查看 / 复制</button>${c.connection.tcp_only ? `<span class="hint">SOCKS5 链路仅支持 TCP</span>` : ""}</div>` : ""}
        <div class="path">${path.join(`<span class="arrow">→</span>`)}</div>
        ${c.hops.length > 1 && !c.deleting ? `<a class="quality-link" href="#/quality/configs/${c.id}">网络质量与趋势 →</a>` : ""}
        ${pendingLine(c)}
      </div>`;
  };
  const summary = (cs) =>
    countText(cs.filter((c) => chainStatus(c) === "applying").length, "下发中", "t-yellow") +
    countText(cs.filter((c) => c.deleting).length, "删除中", "t-red");
  const tools = d.chains.length ? filterBar("chains", [
    filterInput("q", "搜索别名", "search"),
    filterSelect("entry", "全部入口节点", nodeOptions(d.nodes, d.node_groups)),
    filterSelect("via", "经过任意节点", nodeOptions(d.nodes, d.node_groups)),
    filterInput("target", "目标地址"),
    filterSelect("status", "全部状态", plainOptions([["applied", "已应用"], ["saved", "已保存"], ["applying", "下发中"], ["error", "异常"], ["deleting", "删除中"]])),
    d.groups.length > 1 ? filterSelect("group", "全部分组", groupOptions(d.groups)) : "",
  ]) : "";
  let body = groupedList("chains", items, card, summary);
  if (!items.length && isFiltering("chains")) body = emptyState("⌕", "没有匹配的配置。");
  else if (!d.chains.length && d.groups.length === 1) body = emptyState("⇄", "还没有配置，选择链路组合或直连目标。");
  render(listHead("chains", d.chains.length, items.length, "chain-new", "新增配置") + tools + body);
  syncFilters();
}

// ------------------------------------------------------------ 设置

async function settingsPage(request) {
  const s = await api("GET", "settings");
  if (request !== viewRequest) return;
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
        <span>服务端</span><span>${esc(s.version)}</span>
        <span>主动 Agent</span><span>${esc(s.components.active_agent || "-")}</span>
        <span>被动 Agent</span><span>${esc(s.components.passive_agent || "-")}</span>
        <span>旧版兼容 Agent</span><span>${esc(s.components.forward_agent)}</span>
      </div>
    </div>`);
}

// ------------------------------------------------------------ 弹窗

let modalState = null;
const modalValues = () => JSON.stringify([...document.querySelectorAll('#modal form input, #modal form select, #modal form textarea')]
  .filter(el => !el.hasAttribute('data-ignore-dirty')).map(el => [el.name || el.id, el.type === 'checkbox' ? el.checked : el.value]));
const modalChanged = () => modalState && (modalState.dirty || modalValues() !== modalState.initial);

function openModal(html, preserve = false) {
  if (modalState?.busy) return;
  const m = $("#modal");
  if (!preserve || !modalState) {
    const trigger = document.activeElement;
    closeModal();
    modalState = {trigger, dirty:false};
  }
  const scroll = preserve ? m.scrollTop : 0;
  const focused = preserve ? document.activeElement : null;
  const row = focused?.closest('[data-order]')?.dataset.order, action = focused?.dataset.action;
  m.innerHTML = `<div class="modal-box" role="dialog" aria-modal="true" aria-labelledby="modal-title" tabindex="-1">${html}</div>`;
  m.hidden = false;
  const heading = m.querySelector('h2'); if (heading) heading.id = 'modal-title';
  if (!preserve) modalState.initial = modalValues();
  const restore = row != null && action ? m.querySelector(`[data-order="${CSS.escape(row)}"] [data-action="${CSS.escape(action)}"]:not(:disabled)`) : null;
  const first = [...m.querySelectorAll('input:not([data-ignore-dirty]), select:not([hidden]), button:not(:disabled)')].find(el=>el.getClientRects().length);
  (restore || first || m.querySelector('.modal-box'))?.focus();
  m.scrollTop = scroll;
}

function closeModal(result = false) {
  const m = $("#modal"), state = modalState;
  state?.confirm?.finish(false);
  modalState = null;
  m.hidden = true;
  m.innerHTML = "";
  state?.cleanup?.();
  state?.onClose?.(result);
  state?.discard?.(false);
  if (state?.trigger?.isConnected) state.trigger.focus({preventScroll:true});
}

function requestCloseModal() {
  if (modalState?.busy) return Promise.resolve(false);
  if (modalState?.confirm) { modalState.confirm.finish(false); return Promise.resolve(false); }
  if (!modalChanged()) { closeModal(); return Promise.resolve(true); }
  if (modalState.discardPromise) return modalState.discardPromise;
  const box = $('#modal .modal-box'), previous = document.activeElement;
  box.inert = true;
  $('#modal').insertAdjacentHTML('beforeend', `<div class="discard-shade"><div class="modal-box" role="dialog" aria-modal="true" aria-labelledby="discard-title"><h2 id="discard-title">放弃未保存的修改？</h2><div class="form-actions"><button data-discard="no">继续编辑</button><button class="primary danger-bg" data-discard="yes">放弃修改</button></div></div></div>`);
  const state = modalState;
  state.discardPromise = new Promise(resolve => state.discard = resolve);
  $('#modal [data-discard=no]').focus();
  $('#modal .discard-shade').addEventListener('click', e => {
    const action = e.target.closest('[data-discard]')?.dataset.discard;
    if (!action) return;
    const resolve = state.discard; state.discard = null; state.discardPromise = null;
    if (action === 'yes') closeModal();
    else { $('#modal .discard-shade').remove(); box.inert = false; previous?.focus(); }
    resolve(action === 'yes');
  });
  return state.discardPromise;
}

function markModalDirty() { if (modalState) modalState.dirty = true; }

async function withBusyForm(form, work) {
  const session = form.closest('#modal') ? modalState : null;
  if (form.dataset.busy || session?.busy) return;
  form.dataset.busy = '1';
  form.inert = true;
  form.setAttribute('aria-busy', 'true');
  if (session) session.busy = true;
  const button = form.querySelector('button.primary'), label = button?.textContent;
  if (button) { button.disabled = true; button.textContent = '处理中…'; }
  try { return await work(); }
  finally {
    delete form.dataset.busy;
    form.inert = false;
    form.removeAttribute('aria-busy');
    if (session) session.busy = false;
    if (button) { button.disabled = false; button.textContent = label; }
  }
}

// Keep a real select as the form value; searching never changes the selection.
function resourceSelect(name, items, value = '', placeholder = '请选择', byId = false) {
  const options = [{id:'',name:placeholder}, ...items];
  const selected = options.find(x => String(x.id) === String(value ?? '')) || options[0];
  return `<details class="resource-picker"><summary>${esc(selected.name)}</summary><div class="resource-menu"><input type="search" data-resource-search data-ignore-dirty placeholder="搜索名称或地址" aria-label="搜索资源"><div class="resource-options">${options.map(x => `<button type="button" data-resource-value="${esc(x.id)}" data-search="${esc((x.name+' '+(x.addr||'')).toLowerCase())}" aria-pressed="${String(x.id)===String(selected.id)}" ${x.disabled?'disabled':''}>${esc(x.name)}${x.addr||x.note?`<small>${esc([x.addr,x.note].filter(Boolean).join(' · '))}</small>`:''}</button>`).join('')}<span class="resource-empty" hidden>没有匹配项</span></div></div><select hidden ${byId?'id':'name'}="${name}">${options.map(x => `<option value="${esc(x.id)}" ${String(x.id)===String(selected.id)?'selected':''} ${x.disabled?'disabled':''}>${esc(x.name)}</option>`).join('')}</select></details>`;
}

document.addEventListener('input', e => {
  if (!e.target.hasAttribute('data-resource-search')) return;
  const menu = e.target.closest('.resource-menu'), q = e.target.value.trim().toLowerCase();
  let count = 0;
  menu.querySelectorAll('[data-search]').forEach(el => { el.hidden = !el.dataset.search.includes(q); if(!el.hidden)count++; });
  const empty = menu.querySelector('.resource-empty'); if(empty)empty.hidden = count > 0;
});
document.addEventListener('click', e => {
  document.querySelectorAll('.more-actions[open]').forEach(menu => {
    if (!menu.contains(e.target) || e.target.closest('[data-action]')) {
      menu.open = false;
      openDetails.delete(menu.dataset.key);
    }
  });
  const choice = e.target.closest('[data-resource-value]');
  if (choice && (choice.disabled || choice.closest('[inert]') || modalState?.busy)) return;
  if (choice) {
    const picker = choice.closest('.resource-picker'), select = picker.querySelector('select');
    select.value = choice.dataset.resourceValue;
    picker.querySelector('summary').textContent = select.selectedOptions[0].textContent;
    picker.querySelectorAll('[data-resource-value]').forEach(el => el.setAttribute('aria-pressed', el===choice));
    select.dispatchEvent(new Event('change', {bubbles:true}));
    picker.open = false; picker.querySelector('summary').focus();
  }
  document.querySelectorAll('.resource-picker[open]').forEach(el => { if(!el.contains(e.target))el.open=false; });
});
document.addEventListener('toggle', e => {
  if(e.target.matches?.('.resource-picker[open]'))e.target.querySelector('input').focus();
}, true);
window.addEventListener('beforeunload', e => { if(modalChanged() || modalState?.busy || sortChanged() || sortBusy) {e.preventDefault();e.returnValue='';} });

function confirmBox(title, text, okLabel = "确定", danger = false) {
  if (modalState?.busy) return Promise.resolve(false);
  if (modalState?.confirm) return modalState.confirm.promise;
  if (modalState) {
    const state = modalState, box = $('#modal > .modal-box'), focused = document.activeElement;
    const shade = document.createElement('div');
    shade.className = 'discard-shade';
    shade.innerHTML = `<div class="modal-box" role="dialog" aria-modal="true" aria-labelledby="confirm-title"><h2 id="confirm-title">${esc(title)}</h2><p>${text}</p><div class="form-actions"><button type="button" data-confirm="0">取消</button><button type="button" class="primary ${danger?'danger-bg':''}" data-confirm="1">${esc(okLabel)}</button></div></div>`;
    box.inert = true;
    $('#modal').append(shade);
    let resolve;
    const promise = new Promise(done => resolve = done);
    const finish = result => {
      state.confirm = null;
      shade.remove(); box.inert = false;
      if (focused?.isConnected) focused.focus({preventScroll:true});
      resolve(result);
    };
    state.confirm = {promise, finish};
    shade.addEventListener('click', e => {
      const button = e.target.closest('[data-confirm]');
      if (button || e.target === shade) finish(button?.dataset.confirm === '1');
    });
    shade.querySelector('[data-confirm="0"]').focus();
    return promise;
  }
  return new Promise((resolve) => {
    openModal(`
      <h2>${esc(title)}</h2>
      <p>${text}</p>
      <div class="form-actions">
        <button type="button" data-confirm="0">取消</button>
        <button type="button" class="${danger ? "primary danger-bg" : "primary"}" data-confirm="1">${esc(okLabel)}</button>
      </div>`);
    modalState.onClose = resolve;
    $("#modal").querySelectorAll("[data-confirm]").forEach((b) =>
      b.addEventListener("click", () => {
        closeModal(b.dataset.confirm === "1");
      }));
  });
}

function keyModal(title, key, extra = "") {
  openModal(`
    <h2>${esc(title)}</h2>
    <p>${currentNode?.installed_agent === "legacy" ? "旧版节点在 sudo ictl 菜单 1 中更新接入密钥。" : "在节点运行 <code>sudo ictl --credentials</code> 更新接入密钥（需要 0.4.3 或更新的管理器）。"}</p>
    <div class="key-box"><code>${esc(key)}</code><button class="small" data-action="copy" data-text="${esc(key)}">复制</button></div>
    ${extra}
    <div class="form-actions"><button type="button" class="primary" data-action="modal-close">完成</button></div>`);
}

// 分组下拉；新增时默认选中默认分组
function groupSelect(groups, selected) {
  if (groups.length < 2) return "";
  selected ??= groups.find((g) => g.builtin).id;
  const opts = groups.map((g) => `<option value="${g.id}" ${g.id === selected ? "selected" : ""}>${esc(g.name)}</option>`);
  return `<label><span>分组</span><select name="group_id">${opts.join("")}</select></label>`;
}

async function nodeForm(node) {
  const page = pageVersion;
  const { groups } = await api("GET", "nodes");
  if (page !== pageVersion) return;
  openModal(`
    <h2>${node ? "编辑节点" : "新增节点"}</h2>
    <form data-form="node" data-id="${node ? node.id : ""}" data-kind="forward">
      <label><span>名称</span><input name="name" value="${esc(node?.name)}" maxlength="64" required></label>
      ${groupSelect(groups, node?.group_id)}
      <label><span>备注（选填，节点的说明）</span><textarea name="note" rows="3" maxlength="500">${esc(node?.note)}</textarea></label>
      <label><span>入站地址（IPv4 或域名，其他节点通过它连接到该节点）</span>
        <input name="addr" value="${esc(node?.addr)}" class="mono" required></label>
      <label><span>允许的入站端口（端口或范围，多个用逗号分隔，如 10000-20000,30000）</span>
        <input name="ports" value="${esc(node?.ports ?? "10000-65535")}" class="mono" required></label>
      <label><span>质量探测 UDP 端口（默认 33445，0 停止全部探测；仅主动节点使用）</span><input name="quality_port" type="number" min="0" max="65535" value="${node?.quality_port ?? 33445}" required></label>
      <p class="hint">请在防火墙／云安全组放行其他探测节点访问此端口。探测异常不影响现有转发。</p>
      <label><span>同步模式</span><select name="desired_mode" ${node?.first_seen && node.protocol_version < 2 ? "disabled" : ""}>
        <option value="active" ${node?.desired_mode !== "passive" ? "selected" : ""}>主动（长连接自动同步）</option>
        <option value="passive" ${node?.desired_mode === "passive" ? "selected" : ""}>被动（默认手动，可选远程唤醒）</option>
      </select></label>
      <p class="hint">${node?.first_seen && node.protocol_version < 2 ? "请先升级 Agent 并同步一次，才能切换模式。" : "切换模式后需在本机运行 sudo ictl --sync 确认；被动转主动也需要本机操作。"}</p>
      <label><span>被动节点远程唤醒</span><select name="wake_enabled"><option value="0" ${!node?.wake_enabled ? 'selected' : ''}>关闭（默认）</option><option value="1" ${node?.wake_enabled ? 'selected' : ''}>开启：配置变化时自动唤醒</option></select></label>
      <label><span>唤醒 UDP 端口（与节点本地一致，0 为未设置）</span><input name="wake_port" type="number" min="0" max="65535" value="${node?.wake_port ?? 0}" required></label>
      <p class="hint">先在节点运行 sudo ictl，设置 Server 出口 IP 白名单及监听端口。面板开关不会开启节点监听。30 秒未收到成功回执时提示手动同步；程序升级和模式切换仍需本机操作。</p>
      ${node ? `<p class="hint">修改入站地址会重新生成密钥。</p>` : ""}
      <div class="form-error"></div>
      <div class="form-actions">
        <button type="button" data-action="modal-close">取消</button>
        <button class="primary">保存</button>
      </div>
    </form>`);
}

function groupForm(kind, group) {
  openModal(`
    <h2>${group ? "分组改名" : `新建${KIND_LABEL[kind]}分组`}</h2>
    <form data-form="group" data-kind="${kind}" data-id="${group ? group.id : ""}">
      <label><span>分组名称</span><input name="name" value="${esc(group?.name)}" maxlength="64" required></label>
      <div class="form-error"></div>
      <div class="form-actions">
        <button type="button" data-action="modal-close">取消</button>
        <button class="primary">保存</button>
      </div>
    </form>`);
}

function hopRow(nodes, groups, hop = {}) {
  const options = nodeOptions(nodes, groups, hop.node_id, (n) => `${esc(n.name)}（${esc(n.addr)} · 端口 ${esc(n.ports)}）`);
  return `
    <div class="hop-row">
      <span class="hop-no"></span>
      <select name="hop-node"><option value="">选择节点</option>${options}</select>
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
  const page = pageVersion;
  const [d, rd, td] = await Promise.all([api("GET", "nodes"), api("GET", "routes"), api("GET", "targets")]);
  if (page !== pageVersion) return;
  const routes = rd.routes;
  const targets = td.targets;
  if (!targets.length) return toast("请先在目标页面添加目标", true);
  const selectedRoute = chain ? chain.route_id : routes[0]?.id || null;
  openModal(`
    <h2>${chain ? "编辑配置" : "新增配置"}</h2>
    <form data-form="chain" data-id="${chain?.id || ""}">
      <label><span>名称</span><input name="remark" value="${esc(chain?.remark)}" maxlength="64" required></label>
      ${groupSelect(list.data.groups, chain?.group_id)}
      <label><span>链路模板</span><select name="route_id"><option value="" ${selectedRoute === null ? "selected" : ""}>无链路（直连目标）</option>${routes.map(r => `<option value="${r.id}" ${r.id === selectedRoute ? "selected" : ""}>${esc(r.name)}</option>`).join("")}</select></label>
      <div id="config-ports"></div>
      <label><span>目标</span><select name="target_id" required><option value="">请选择目标</option>${targets.map(t => `<option value="${t.id}" ${t.id === chain?.target_id ? "selected" : ""}>${esc(t.name)} · ${esc(t.host)}:${t.port}</option>`).join("")}</select></label>
      <div class="form-error"></div>
      <div class="form-actions"><button type="button" data-action="modal-close">取消</button><button class="primary">保存</button></div>
    </form>`);
  const form = $("#modal form");
  const updatePorts = () => {
    const r = routes.find(r => r.id === Number(form.elements.route_id.value));
    if (!r) { $("#config-ports", form).innerHTML = ""; return; }
    const old = r.id === chain?.route_id ? [...chain.hops] : [];
    $("#config-ports", form).innerHTML = `<p class="hint">各跳端口属于此配置；留空自动分配。链路修改会自动更新引用它的配置。</p>` + r.node_ids.map((nid, i) => {
      const n = d.nodes.find(n => n.id === nid);
      const index = old.findIndex(h => h.node_id === nid);
      const previous = index >= 0 ? old.splice(index, 1)[0].port : "";
      return `<label><span>${i + 1}. ${esc(n.name)}（允许 ${esc(n.ports)}）</span><input name="config_port" value="${previous}" type="number" min="1" max="65535" placeholder="自动分配"></label>`;
    }).join("");
  };
  form.elements.route_id.addEventListener("change", updatePorts);
  updatePorts();
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
  const slot = err.field && form.querySelector(`[data-error="${CSS.escape(err.field)}"]`);
  const field = err.field === 'target_id' && !form.elements.target_id ? 'target' : err.field?.startsWith('capacity:') ? 'port' : err.field;
  const input = field && form.querySelector(`[name="${CSS.escape(field)}"], #${CSS.escape(field)}`);
  if (slot) {
    slot.textContent = msg;
  } else if (input) {
    input.classList.add("invalid");
    (input.closest('.resource-picker') || input).insertAdjacentHTML("afterend", `<div class="field-error">${esc(msg)}</div>`);
    (input.closest('.resource-picker')?.querySelector('summary') || input).focus();
  } else {
    const fe = $(".form-error", form);
    if (fe) {
      fe.textContent = msg;
      if (err.status === 409 && ['link-save','instance-save','quick-switch'].includes(form.dataset.form))
        fe.insertAdjacentHTML('beforeend', ' <button type="button" class="small" data-action="link-reload">载入最新配置</button>');
    }
    else toast(msg, true);
  }
}

// 表单中的分组；只有默认分组时没有该字段，新增时由服务端放入默认分组，修改时保持不变
const groupId = (data) => (data.group_id ? Number(data.group_id) : undefined);

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
      const r = await api("POST", "nodes", { name: data.name, note: data.note, addr: data.addr, ports: data.ports, desired_mode: data.desired_mode, quality_port: Number(data.quality_port), wake_enabled: data.wake_enabled === '1', wake_port: Number(data.wake_port), group_id: groupId(data) });
      closeModal();
      location.hash = `#/nodes/${r.id}`;
      return;
    }
    const r = await api("PATCH", `nodes/${id}`, { name: data.name, note: data.note, addr: data.addr, ports: data.ports, desired_mode: data.desired_mode, quality_port: Number(data.quality_port), wake_enabled: data.wake_enabled === '1', wake_port: Number(data.wake_port), group_id: groupId(data) });
    closeModal();
    if (r.key) {
      const ups = r.upstream.length ? `<p>以下节点需要同步规则：${r.upstream.map(esc).join("、")}</p>` : "";
      keyModal("入站地址已修改，密钥已重新生成", r.key, ups);
    } else {
      toast("已保存");
    }
    refresh();
  },

  async group(form, data) {
    const id = form.dataset.id;
    if (id) {
      await api("PATCH", `groups/${id}`, { name: data.name });
    } else {
      await api("POST", "groups", { kind: form.dataset.kind, name: data.name });
    }
    closeModal();
    toast("已保存");
    refresh();
  },

  async chain(form, data) {
    const id = form.dataset.id;
    const ports = [...form.querySelectorAll("[name=config_port]")].map(input => input.value);
    const body = { remark: data.remark, route_id: data.route_id ? Number(data.route_id) : null, target_id: Number(data.target_id), ports, group_id: groupId(data) };
    const r = await api(id ? "PATCH" : "POST", id ? `configs/${id}` : "configs", body);
    closeModal();
    toast(r.assigned.length ? `已保存，自动分配端口：${r.assigned.join("、")}` : "配置已保存");
    refresh();
  },
};

document.addEventListener("submit", async (e) => {
  const form = e.target;
  const handler = FORMS[form.dataset.form];
  if (!handler) return;
  e.preventDefault();
  if (form.closest('[inert]') || form.dataset.busy || (form.closest('#modal') && modalState?.busy)) return;
  try {
    clearFormErrors(form);
    const data = Object.fromEntries(new FormData(form));
    await withBusyForm(form, () => handler(form, data));
  } catch (err) {
    if (err instanceof ApiError && (err.status === 401 && form.dataset.form !== "login")) return;
    if (err instanceof ApiError && err.status === 403 && err.data.must_change) return;
    if (form.isConnected) showFormError(form, err);
  }
});

// ------------------------------------------------------------ 按钮操作

const chainById = (id) => list.data.chains.find((c) => c.id === Number(id));

const ACTIONS = {
  copy: (el) => copy(el.dataset.text),
  "modal-close": () => requestCloseModal(),

  async logout() {
    await safe(() => api("POST", "logout"));
    me = null;
    showLogin();
  },

  "node-new": () => nodeForm(),
  "node-edit": () => nodeForm(currentNode),
  async "node-query"(el) {
    const id = currentNode.id, page = pageVersion;
    openModal('<h2>节点查询</h2><p role="status">查询中…</p><button data-action="modal-close">关闭</button>');
    const session = modalState;
    try {
      const result = await api("POST", `nodes/${id}/query`, {kind:el.dataset.kind, rule_id:Number(el.dataset.rule || 0)});
      if (page !== pageVersion || session !== modalState) return;
      openModal(`<h2>节点查询结果</h2><pre style="white-space:pre-wrap;overflow-wrap:anywhere">${esc(JSON.stringify(result.data || result, null, 2))}</pre><button data-action="modal-close">关闭</button>`);
    } catch (err) {
      if (page !== pageVersion || session !== modalState) return;
      openModal(`<h2>节点查询失败</h2><p class="t-red">${esc(err.message)}</p><button data-action="modal-close">关闭</button>`);
    }
  },

  "key-toggle"() {
    const id = currentNode.id;
    revealedKeys.has(id) ? revealedKeys.delete(id) : revealedKeys.add(id);
    refresh();
  },

  async "node-rekey"() {
    const ok = await confirmBox("重置密钥", `重置后旧密钥立即失效，节点 <b>${esc(currentNode.name)}</b> 需要填入新密钥才能继续同步。${currentNode.installed_agent !== "legacy" ? "请先将节点管理器更新到 0.4.3，再执行此操作。" : ""}`, "重置");
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

  "group-toggle"(el) {
    if (isFiltering(list.kind) && !sortMode) return;   // 搜索或筛选时分组临时展开
    const key = el.dataset.group;
    collapsed.has(key) ? collapsed.delete(key) : collapsed.add(key);
    saveCollapsed();
    el.closest(".group").classList.toggle("closed", collapsed.has(key));
  },

  "group-new"(el) {
    groupForm(el.dataset.kind);
  },
  "group-rename"(el) {
    groupForm(list.kind, list.data.groups.find((g) => g.id === Number(el.dataset.id)));
  },
  async "group-delete"(el) {
    const g = list.data.groups.find((x) => x.id === Number(el.dataset.id));
    const count = list.data[list.kind].filter((x) => x.group_id === g.id).length;
    const def = list.data.groups.find((x) => x.builtin);
    const text = `确定删除分组 <b>${esc(g.name)}</b>？` +
      (count ? `组内的 ${count} 个${KIND_LABEL[list.kind]}将移到 <b>${esc(def.name)}</b>。` : "");
    if (!(await confirmBox("删除分组", text, "删除", true))) return;
    await api("DELETE", `groups/${g.id}`);
    collapsed.delete(`${list.kind}-${g.id}`);
    saveCollapsed();
    toast("分组已删除");
    refresh();
  },

  "filter-clear"(el) {
    filters[el.dataset.kind] = {};
    list.draw();
  },

  async "sort-start"(el) {
    const page = pageVersion;
    sortMode = el.dataset.kind;
    sortInitial = null;
    clearTimeout(refreshTimer);
    await refresh();
    if (page === pageVersion && sortMode === el.dataset.kind) sortInitial = sortValues();
  },
  async "sort-cancel"() {
    if (sortBusy || (sortChanged() && !(await confirmBox('放弃未保存的排序？','此次顺序调整将不会保存。','放弃',true)))) return;
    sortMode = null;
    sortInitial = null;
    refresh();
    schedule();
  },
  async "sort-save"(el) {
    if (sortBusy) return;
    const ids = [...app.querySelectorAll(".sort-card")].map((c) => Number(c.dataset.id));
    const groups = list.data.groups.length > 1
      ? [...app.querySelectorAll(".sort-group")].map((g) => Number(g.dataset.id))
      : list.data.groups.map((g) => g.id);
    sortBusy = true;
    app.inert = true;
    try {
      await api("POST", `${el.dataset.kind}/order`, { groups, ids });
      sortMode = null;
      sortInitial = null;
      toast("顺序已保存");
      await refresh();
    } finally {
      sortBusy = false;
      app.inert = false;
      schedule();
    }
  },

  "chain-new": () => chainForm(),
  "chain-edit": (el) => chainForm(chainById(el.dataset.id)),

  async "chain-delete"(el) {
    const c = chainById(el.dataset.id);
    const force = el.dataset.force === "1";
    const text = force
      ? `直接删除配置 <b>${esc(c.remark)}</b> 的记录。尚未确认的节点恢复连接后，会在同步时自动清除对应规则。`
      : c.route_id || c.pending.length
        ? `将通知相关节点移除配置 <b>${esc(c.remark)}</b> 的转发规则，所有节点确认后自动删除。`
        : `删除直连配置 <b>${esc(c.remark)}</b>？目标监听及其他引用配置会保留。`;
    if (!(await confirmBox(force ? "强制删除配置" : "删除配置", text, "删除", true))) return;
    const r = await api("DELETE", `configs/${c.id}${force ? "?force=1" : ""}`);
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
  if (e.target.id === "modal") return void requestCloseModal();
  if (e.target.closest(".drag-handle")) return;   // 拖动分组标题时不触发折叠
  const el = e.target.closest("[data-action]");
  if (!el || !ACTIONS[el.dataset.action] || el.disabled || el.closest('[inert]') || (el.closest('#modal') && modalState?.busy)) return;
  e.preventDefault();
  safe(() => ACTIONS[el.dataset.action](el));
});

document.addEventListener("keydown", (e) => {
  if (e.key === 'Escape') {
    const menu = document.querySelector('.more-actions[open]');
    if (menu) { menu.open=false; openDetails.delete(menu.dataset.key); menu.querySelector('summary').focus(); e.preventDefault(); return; }
  }
  if ($("#modal").hidden) return;
  if (e.target.matches?.('[data-resource-search]') && e.key === 'Enter') {e.preventDefault();return;}
  if (e.key === 'Escape') {
    e.preventDefault();
    const discard = $('#modal [data-discard=no]');
    const picker = $('#modal .resource-picker[open]');
    if (modalState?.confirm) modalState.confirm.finish(false);
    else if (discard) discard.click();
    else if (picker) { picker.open = false; picker.querySelector('summary').focus(); }
    else void requestCloseModal();
  }
  if (e.key === 'Tab') {
    const box = [...document.querySelectorAll('#modal .modal-box')].at(-1);
    const items = [...box.querySelectorAll('button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea, a[href], summary, [tabindex="0"]')].filter(el => el.getClientRects().length);
    const first=items[0], last=items.at(-1);
    if (!first) {e.preventDefault();box.focus();}
    else if (e.shiftKey && (document.activeElement===first || !items.includes(document.activeElement))) {e.preventDefault();last.focus();}
    else if (!e.shiftKey && (document.activeElement===last || !items.includes(document.activeElement))) {e.preventDefault();first.focus();}
  }
});

// ------------------------------------------------------------ 拖动排序
// 只能从手柄开始拖动（触屏上其余区域仍可正常滚动）；拖动时其他卡片平滑让位。
// 卡片只能在所在分组内移动，分组只能在分组之间移动

let drag = null;

document.addEventListener("pointerdown", (e) => {
  const handle = e.target.closest(".drag-handle");
  if (!handle || e.button > 0 || handle.closest('[inert]') || (handle.closest('#modal') && modalState?.busy)) return;
  e.preventDefault();
  const card = handle.closest(".sort-item");
  handle.setPointerCapture(e.pointerId);
  drag = { card, offset: e.clientY - card.getBoundingClientRect().top };
  card.classList.add("dragging");
});

document.addEventListener("pointermove", (e) => {
  if (!drag) return;
  const { card } = drag;
  const parent = card.parentNode;
  const y = e.clientY;
  const scroller = card.closest('#modal');
  if (y < 60) scroller ? scroller.scrollBy(0,-12) : scrollBy(0,-12);
  else if (y > innerHeight - 60) scroller ? scroller.scrollBy(0,12) : scrollBy(0,12);

  const others = [...parent.children].filter((c) => c !== card && c.classList.contains("sort-item"));
  const before = others.find((c) => {
    const r = c.getBoundingClientRect();
    return y < r.top + r.height / 2;
  }) || null;
  if (card.nextElementSibling !== before) {
    const tops = new Map(others.map((c) => [c, c.getBoundingClientRect().top]));
    parent.insertBefore(card, before);
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
  const parent = drag.card.parentElement;
  drag = null;
  parent.dispatchEvent(new Event('reordered', {bubbles:true}));
}
document.addEventListener("pointerup", endDrag);
document.addEventListener("pointercancel", endDrag);

// 记住折叠区的展开状态（toggle 事件不冒泡，用捕获）
document.addEventListener("toggle", (e) => {
  const key = e.target.dataset?.key;
  if (!key) return;
  e.target.open ? openDetails.add(key) : openDetails.delete(key);
}, true);

document.addEventListener("input", (e) => {
  const name = e.target.dataset?.filter;
  if (!name || !list) return;
  filters[list.kind][name] = e.target.value;
  list.draw();
});

document.addEventListener("change", (e) => {
  if (e.target.name !== "theme") return;
  localStorage.setItem("ictl-theme", e.target.value);
  document.documentElement.dataset.theme = e.target.value;
});

// ------------------------------------------------------------ 链路与目标

async function routeList(request) {
  const data = await api("GET", "routes");
  if (request !== viewRequest) return;
  list = {kind: "routes", data, draw: drawRoutes};
  drawRoutes();
}

function drawRoutes() {
  const d = list.data;
  const card = r => `<div class="card" data-key="route-${r.id}"><div class="card-head"><div class="title">${esc(r.name)}</div><div class="actions"><button class="small" data-action="route-edit" data-id="${r.id}">编辑</button><button class="small danger" data-action="route-delete" data-id="${r.id}">删除</button></div></div>
      <div class="path">${r.nodes.map(n => `<a href="#/nodes/${n.id}">${esc(n.name)}</a>`).join('<span class="arrow">→</span>')}</div>
      ${r.nodes.length > 1 ? `<a class="quality-link" href="#/quality/routes/${r.id}">网络质量与趋势 →</a>` : ""}
      <div class="sub">${r.configs.length ? `被 ${r.configs.length} 个配置引用：${r.configs.map(c => esc(c.remark)).join("、")}` : "尚未被配置使用"}</div></div>`;
  drawManagedList("routes", d.routes, card, "route-new", "新增链路", "链路保存转发节点顺序，不分配端口。修改后会自动更新所有引用配置。");
}

const qualityViews = new Map();
let qualityResizeObserver = null;
let qualityResizeFrame = null;

function releaseQuality() {
  qualityResizeObserver?.disconnect();
  qualityResizeObserver = null;
  cancelAnimationFrame(qualityResizeFrame);
  qualityResizeFrame = null;
}
const QUALITY_STATES = {unknown: "未知", ok: "成功", timeout: "探测超时", error: "探测异常", offline: "节点离线", stopped: "探测已停止", unsupported: "需要升级 Agent", disabled: "探测已关闭", waiting: "等待探测就绪", missing: "未采样", collecting: "持续采集中"};
const qualityNumber = (n, unit = " ms") => n == null ? "—" : Number(n).toFixed(1) + unit;

const QUALITY_PROTOCOLS = {"tcp-connect-v1": ["TCP", "var(--yellow)"], "udp-echo-v1": ["UDP", "var(--blue)"], "icmp-echo-v1": ["ICMP", "var(--green)"]};
const QUALITY_PLOT = {height:250, left:55, right:16, top:18, bottom:40};

function qualityLegend(series) {
  return `<div class="quality-legend">${series.map(s=>{
    const [name,color]=QUALITY_PROTOCOLS[s.protocol];
    return `<button type="button" class="quality-key" data-quality-focus="${s.protocol}" style="--series-color:${color}" aria-label="高亮 ${name} 曲线">${name}</button>`;
  }).join('')}</div>`;
}

function qualityTable(series, now) {
  const rows=series.map(s=>{
    const [name,color]=QUALITY_PROTOCOLS[s.protocol], stat=s.summary, latest=s.latest;
    const fresh=latest && now-latest.slot<=25 && (s.path_key == null || latest.path_key===s.path_key);
    const state=['collecting','ok'].includes(s.state) && fresh ? latest.status : s.state;
    const tone=state==='ok'?'green':['error','offline'].includes(state)?'red':state==='timeout'?'yellow':state==='collecting'?'blue':'grey';
    const value=(key,text,cls='')=>`<span class="${cls}" data-quality-value="${s.protocol}:${key}">${text}</span>`;
    return `<tr data-quality-focus="${s.protocol}"><td><span class="quality-key" style="--series-color:${color}">${name}</span></td>
      <td>${value('latest',fresh&&latest.status==='ok'?qualityNumber(latest.rtt):'—','quality-latest')}</td>
      <td>${value('mean',qualityNumber(stat.mean))}</td><td>${value('p95',qualityNumber(stat.p95))}</td><td>${value('jitter',qualityNumber(stat.jitter))}</td>
      <td>${value('loss',qualityNumber(stat.loss,'%'),stat.loss>0?'t-yellow':'')}<small class="quality-time">${stat.samples?`${stat.lost} / ${stat.samples} 个样本`:''}</small></td>
      <td title="${esc(s.note||latest?.error||'')}"><span class="pill t-${tone}" data-quality-value="${s.protocol}:state">${esc(QUALITY_STATES[state]||state)}</span><small class="quality-time">${latest?esc(clock(latest.slot)):''}</small></td></tr>`;
  }).join('');
  return `<div class="quality-table"><table><thead><tr><th>探测</th><th>最新延迟</th><th>5 分钟平均</th><th>P95</th><th>波动</th><th>超时率</th><th>状态</th></tr></thead><tbody>${rows}</tbody></table></div>`;
}

function qualityChart(series, now, seconds, width = app.clientWidth - 42) {
  const {height,left,right,top,bottom}=QUALITY_PLOT;
  width = Math.max(160, Math.round(width));
  series = series.map(s => ({...s, points:s.points.filter(p => p.time >= now-seconds && p.time <= now)}));
  const maximum = Math.max(1, ...series.flatMap(s => s.points.map(p => p.peak ?? p.mean ?? 0))) * 1.1;
  const x = t => left + (t - (now - seconds)) / seconds * (width - left - right);
  const y = n => top + (height - top - bottom) * (1 - n / maximum);
  const grid = [0, .25, .5, .75, 1].map(f => `<line x1="${left}" x2="${width-right}" y1="${y(maximum*f)}" y2="${y(maximum*f)}" stroke="var(--border)" stroke-dasharray="3 5"/><text x="${left-8}" y="${y(maximum*f)+4}" text-anchor="end">${(maximum*f).toFixed(1)}</text>`).join("");
  const ticks = [0, .5, 1].map(f => { const time = now-seconds+seconds*f; return `<text x="${x(time)}" y="${height-5}" text-anchor="${f === 0 ? "start" : f === 1 ? "end" : "middle"}">${esc(clock(time))}</text>`; }).join("");
  const changes = new Set();
  const curves = series.map((s, index) => {
    const [name, color] = QUALITY_PROTOCOLS[s.protocol];
    let previous = null, pathKey = null, elements = "";
    for (const p of s.points) {
      if(p.path_key && pathKey && pathKey!==p.path_key && !changes.has(p.time)) {
        changes.add(p.time);elements+=`<line x1="${x(p.time)}" x2="${x(p.time)}" y1="${top}" y2="${height-bottom}" stroke="var(--muted)" stroke-dasharray="3 4"><title>路径切换 · ${esc(p.path_label||'')}</title></line>`;
      }
      if(p.path_key)pathKey=p.path_key;
      const title = `<title>${name} · ${esc(fmtTime(p.time))} · ${esc(QUALITY_STATES[p.status] || p.status)} · 平均 ${qualityNumber(p.mean)} · 峰值 ${qualityNumber(p.peak)} · 探测超时 ${qualityNumber(p.loss, "%")}（${p.lost}/${p.samples}）${p.resolved_ip ? " · " + esc(p.resolved_ip) : ""}${p.error ? " · " + esc(p.error) : ""}</title>`;
      const px = x(p.time).toFixed(1);
      if (p.failures > 0 || (p.status !== "ok" && p.status !== "missing")) {
        const py = height - bottom + 8 + index * 8;
        elements += `<path d="M ${x(p.time)-2},${py-2} l 4,4 m -4,0 l 4,-4" stroke="${color}" stroke-width="1.5">${title}</path>`;
      }
      if (p.mean == null) {
        // A bucket can be empty when nominal 10 s probes arrive slightly late.
        // Keep the previous sample only when real timestamps can bound the gap.
        if (p.status !== "missing" || previous?.last_slot == null) previous = null;
        continue;
      }
      const continuous = previous && (p.first_slot == null || previous.last_slot == null || p.first_slot - previous.last_slot <= 20);
      if (continuous && !previous.gap && !p.gap && !previous.endpoint_change && !p.endpoint_change && previous.resolved_ip === p.resolved_ip && previous.path_key === p.path_key) {
        elements += `<line x1="${x(previous.time).toFixed(1)}" y1="${y(previous.mean).toFixed(1)}" x2="${px}" y2="${y(p.mean).toFixed(1)}" stroke="${color}" stroke-width="2"/>`;
      }
      if (p.peak != null && p.peak !== p.mean) elements += `<line x1="${px}" x2="${px}" y1="${y(p.mean).toFixed(1)}" y2="${y(p.peak).toFixed(1)}" stroke="${color}" opacity=".3">${title}</line>`;
      elements += `<circle cx="${px}" cy="${y(p.mean).toFixed(1)}" r="2.5" fill="${color}">${title}</circle>`;
      previous = p;
    }
    return `<g class="quality-series" data-protocol="${s.protocol}">${elements}</g>`;
  }).join("");
  const version=esc(JSON.stringify([seconds,series.map(s=>s.latest||s.points.at(-1))]));
  return `<div class="quality-plot" data-version="${version}"><svg class="quality-chart" viewBox="0 0 ${width} ${height}" tabindex="0" role="img" aria-label="网络延迟趋势，使用左右方向键查看数据">${grid}${curves}${ticks}<line class="quality-cursor" y1="${top}" y2="${height-bottom}" hidden/></svg><div class="quality-tooltip" role="status" hidden></div></div>`;
}

function hideQualityPoint(plot) {
  delete plot.qualityTime;
  plot.querySelector('.quality-cursor').setAttribute('hidden','');
  plot.querySelector('.quality-tooltip').hidden=true;
}

function showQualityPoint(plot, time) {
  const {series,now,seconds,times}=plot.qualityData;
  if(!times.length)return;
  time=times.reduce((a,b)=>Math.abs(b-time)<Math.abs(a-time)?b:a);
  plot.qualityTime=time;
  const line=plot.querySelector('.quality-cursor'), tip=plot.querySelector('.quality-tooltip'), svg=plot.querySelector('svg');
  const {left,right}=QUALITY_PLOT, width=svg.viewBox.baseVal.width, x=left+(time-now+seconds)/seconds*(width-left-right);
  line.setAttribute('x1',x);line.setAttribute('x2',x);line.removeAttribute('hidden');
  tip.innerHTML=`<strong>${esc(fmtTime(time))}</strong>${series.map(s=>{
    const p=s.points.find(p=>p.time===time || (p.time<time && time<p.end)),[name,color]=QUALITY_PROTOCOLS[s.protocol];
    return `<div><span class="quality-key" style="--series-color:${color}">${name}</span><b>${qualityNumber(p?.mean)}</b></div><small>${esc(QUALITY_STATES[p?.status||'missing']||p?.status)}${p?.mean!=null?` · 峰值 ${qualityNumber(p.peak)}`:''}${p?.loss!=null?` · 超时 ${qualityNumber(p.loss,'%')}`:''}</small>`;
  }).join('')}`;
  tip.hidden=false;
  const matrix=svg.getScreenCTM(), offset=matrix.a*x+matrix.e-plot.getBoundingClientRect().left;
  tip.style.left=`${Math.max(8,Math.min(offset+14,plot.clientWidth-tip.offsetWidth-8))}px`;
}

function renderQuality(html, charts) {
  releaseQuality();
  const key=el=>el.closest('.quality-card').dataset.key+'/'+(el.dataset.qualityValue||'plot');
  const before=new Map([...app.querySelectorAll('.quality-card [data-quality-value],.quality-card .quality-plot')].map(el=>[key(el),el.dataset.version||el.textContent]));
  render(html);
  if(!matchMedia('(prefers-reduced-motion: reduce)').matches) {
    app.querySelectorAll('.quality-card [data-quality-value],.quality-card .quality-plot').forEach(el=>{
      if(before.has(key(el)) && before.get(key(el))!==(el.dataset.version||el.textContent))
        el.animate([{opacity:.45},{opacity:1}],{duration:240,easing:'ease-out'});
    });
  }
  qualityResizeObserver = new ResizeObserver(() => {
    if (qualityResizeFrame != null) return;
    // Changing the viewBox also changes SVG height; draw outside the observer
    // callback so its own resize notification cannot loop in the same frame.
    qualityResizeFrame = requestAnimationFrame(() => {
      qualityResizeFrame = null;
      app.querySelectorAll('.quality-plot').forEach(plot => resizeQualityPlot(plot,plot.clientWidth));
    });
  });
  app.querySelectorAll('.quality-plot').forEach((plot,i)=>{
    const data=charts[i], svg=plot.querySelector('svg');
    const series=data.series.map(s=>({...s,points:s.points.filter(p=>p.time>=data.now-data.seconds && p.time<=data.now)}));
    plot.qualityData={...data,series,times:[...new Set(series.flatMap(s=>s.points.map(p=>p.time)))].sort((a,b)=>a-b)};
    resizeQualityPlot(plot,plot.clientWidth);
    qualityResizeObserver.observe(plot);
    const point=e=>{
      const cursor=svg.createSVGPoint();cursor.x=e.clientX;cursor.y=e.clientY;
      const x=cursor.matrixTransform(svg.getScreenCTM().inverse()).x;
      const {left,right}=QUALITY_PLOT,width=svg.viewBox.baseVal.width;
      if(x<left || x>width-right)return hideQualityPoint(plot);
      showQualityPoint(plot,data.now-data.seconds+(x-left)/(width-left-right)*data.seconds);
    };
    svg.onpointermove=point;svg.onpointerdown=point;
    svg.onpointerleave=()=>hideQualityPoint(plot);svg.onblur=()=>hideQualityPoint(plot);
    svg.onkeydown=e=>{
      if(e.key==='Escape')return hideQualityPoint(plot);
      if(!['ArrowLeft','ArrowRight','Home','End'].includes(e.key))return;
      e.preventDefault();
      const times=plot.qualityData.times;if(!times.length)return;
      const current=plot.qualityTime==null?times.length-1:times.indexOf(plot.qualityTime);
      const index=e.key==='Home'?0:e.key==='End'?times.length-1:Math.max(0,Math.min(times.length-1,current+(e.key==='ArrowLeft'?-1:1)));
      showQualityPoint(plot,times[index]);
    };
    if(plot.qualityTime!=null)showQualityPoint(plot,plot.qualityTime);
  });
}

function resizeQualityPlot(plot, width) {
  if (!plot.isConnected || width <= 0) return;
  width = Math.max(160,Math.round(width));
  const svg = plot.querySelector('svg');
  if (svg.viewBox.baseVal.width === width) return;
  const {series,now,seconds} = plot.qualityData;
  const template = document.createElement('template');
  template.innerHTML = qualityChart(series,now,seconds,width);
  const next = template.content.querySelector('svg');
  svg.setAttribute('viewBox',next.getAttribute('viewBox'));
  morph(svg,next);
  if (plot.qualityTime != null) showQualityPoint(plot,plot.qualityTime);
}

async function qualityPage(scope, id, request) {
  if(scope==='links')return linkQualityPage(id,request);
  const key = `${scope}/${id}`, selection = qualityViews.get(key) || {range: 900};
  let d;
  try { d = await api("POST", "quality", {scope, id, ...selection}); }
  catch (err) {
    if (request !== viewRequest || err.status === 401 || err.status === 403) return;
    if (err.status === 400 && selection.source != null) {
      qualityViews.set(key, {range: selection.range});
      return qualityPage(scope, id, request);
    }
    if (request === viewRequest) render(`<h1>网络质量</h1><p class="t-red">${esc(err.message)}</p><a href="#/${scope === "links" ? "routes/" + id : scope}">返回列表</a>`);
    return;
  }
  if (request !== viewRequest) return;
  if (!d.pair) return render(`<h1>网络质量</h1><p>至少需要两个节点。</p><a href="#/${scope === "links" ? "routes/" + id : scope}">返回列表</a>`);
  qualityViews.set(key, {range: selection.range, source: d.pair.source, target: d.pair.target});
  const options = value => d.nodes.map(n => `<option value="${n.id}" ${n.id === value ? "selected" : ""}>${esc(n.name)}${n.mode === "passive" ? "（被动）" : ""}</option>`).join("");
  const notes = d.series.filter(s => s.note || s.latest?.error).map(s => `${QUALITY_PROTOCOLS[s.protocol][0]}：${esc(s.note || s.latest.error)}`);
  renderQuality(`<div class="list-head"><h1>网络质量</h1><a href="#/${scope === "links" ? "routes/" + id : scope}">返回列表</a></div>
    <div class="panel quality-controls" data-quality="${key}">
      <label><span>发起节点</span><select name="quality-source">${options(d.pair.source)}</select></label>
      <span class="quality-arrow">→</span><label><span>接收节点</span><select name="quality-target">${options(d.pair.target)}</select></label>
      <label><span>时间范围</span><select name="quality-range">${[[900,"最近 15 分钟"],[3600,"最近 1 小时"],[86400,"最近 24 小时"]].map(([n,label]) => `<option value="${n}" ${n === selection.range ? "selected" : ""}>${label}</option>`).join("")}</select></label>
    </div>
    <div class="panel quality-card" data-key="quality-${scope}-${id}"><div class="quality-heading"><h2>往返延迟 <span class="count">ms</span></h2>${qualityLegend(d.series)}</div>
      ${qualityChart(d.series,d.now,selection.range)}
      ${qualityTable(d.series,d.now)}
      ${notes.length ? `<p class="hint">${notes.join("；")}</p>` : ""}
    </div>
    <p class="hint">${d.adjacent ? "后台持续采集" : "查看期间采集非相邻节点的直达网络"}，每种协议默认 10 秒一次，共享节点限速。历史保留 24 小时。底部 × 表示异常或未知。</p>`,[{series:d.series,now:d.now,seconds:selection.range}]);

}

document.addEventListener("change", e => {
  const box = e.target.closest?.("[data-quality]");
  if (!box) return;
  const source = Number(box.querySelector('[name="quality-source"]').value);
  const target = Number(box.querySelector('[name="quality-target"]').value);
  if (source === target) { toast("请选择两个不同节点", true); return; }
  qualityViews.set(box.dataset.quality, {source, target, range: Number(box.querySelector('[name="quality-range"]').value)});
  refresh();
});

// 各列表共用交互方式，数据、分组 ID、筛选与折叠状态按 kind 分开。
function drawManagedList(kind, rows, card, newAction, newLabel, hint, extraActions = "") {
  if (sortMode === kind) return render(listHead(kind, rows.length, rows.length) +
    sortList(kind, rows, item => `<div class="title">${esc(item.name)}</div>`));
  const f = filters[kind], groups = list.data.groups;
  const items = rows.filter(item => (!f.q || has(item.name, f.q)) && (!f.group || item.group_id === Number(f.group)));
  const controls = filterBar(kind, [filterInput("q", "搜索名称", "search"),
    groups.length > 1 ? filterSelect("group", "全部分组", groupOptions(groups)) : ""]);
  const body = !items.length && isFiltering(kind) ? emptyState("⌕", "没有匹配的记录。")
    : !rows.length && groups.length === 1 ? emptyState("◎", `还没有${KIND_LABEL[kind]}，点击右上角新增。`)
    : groupedList(kind, items, card, () => "");
  render(listHead(kind, rows.length, items.length, newAction, newLabel, extraActions) + (hint?`<p class="hint">${hint}</p>`:'') + controls + body);
  syncFilters();
}

async function routeForm(r) {
  const page = pageVersion;
  const d = await api("GET", "nodes");
  if (page !== pageVersion) return;
  const nodes = d.nodes.filter(n => n.kind === "forward" && n.status !== "deleting");
  if (!nodes.length) return toast("请先添加转发节点", true);
  const row = id => `<div class="hop-row route-hop"><span class="hop-no"></span><select name="route-node" required><option value="">选择节点</option>${nodeOptions(nodes, d.groups, id)}</select><button type="button" data-action="hop-up">↑</button><button type="button" data-action="hop-down">↓</button><button type="button" data-action="hop-del">✕</button></div>`;
  openModal(`<h2>${r ? "编辑链路" : "新增链路"}</h2><form data-form="route" data-id="${r?.id || ""}">
    <label><span>名称</span><input name="name" value="${esc(r?.name)}" maxlength="64" required></label>
    ${groupSelect(list.data.groups, r?.group_id)}
    <div id="hop-list">${(r?.node_ids || [null]).map(row).join("")}</div><button type="button" class="small" data-action="hop-add">添加一跳</button>
    ${r?.configs.length ? `<p class="t-yellow">保存会自动更新 ${r.configs.length} 个配置：${r.configs.map(c => esc(c.remark)).join("、")}。调整入口节点会改变连接地址。</p>` : ""}
    <div class="form-error"></div><div class="form-actions"><button type="button" data-action="modal-close">取消</button><button class="primary">保存</button></div></form>`);
  $("#modal form").hopTemplate = () => row(null); renumberHops();
}

const PROTOCOLS = {shadowsocks: "Shadowsocks", "vless-reality": "VLESS Reality", endpoint: "IP／域名＋端口"};
async function targetList(request) {
  const data = await api("GET", "targets");
  if (request !== viewRequest) return;
  list = {kind: "targets", data, draw: drawTargets};
  drawTargets();
}

function drawTargets() {
  const card = t => `<div class="card target-row" data-key="target-${t.id}"><div class="collection-main inline-summary"><button class="plain-title" data-action="target-edit" data-id="${t.id}" title="${esc(t.name)}">${esc(t.name)}</button><div class="sub"><span>${esc(t.type==='endpoint'?'地址':PROTOCOLS[t.type])}</span> · <span class="mono">${esc(t.host)}:${t.port}</span></div></div><span class="collection-meta muted" title="${esc(t.configs.map(c=>c.remark).join('、'))}">${t.configs.length?`${t.configs.length} 个引用`:'未引用'}</span><div class="row-actions"><button class="small" data-action="copy" data-text="${esc(t.uri||t.host+':'+t.port)}">复制</button><button class="small" data-action="target-edit" data-id="${t.id}">编辑</button>${moreActions(`target-actions-${t.id}`,`<button class="danger" data-action="target-delete" data-id="${t.id}">删除</button>`)}</div></div>`;
  drawManagedList("targets", list.data.targets, card, "target-new", "添加地址", "",
    `<button data-action="target-import" title="导入 SS 或 VLESS 分享链接">导入</button>`);
}

function targetForm(t, imported = false) {
  imported = t ? t.type !== "endpoint" : imported;
  openModal(`<h2>${t ? "编辑目标" : imported ? "导入目标" : "添加地址目标"}</h2><form data-form="target" data-id="${t?.id || ""}" data-imported="${imported}">
    <label><span>名称${imported ? "（留空使用链接名称）" : ""}</span><input name="name" value="${esc(t?.name)}" maxlength="64" ${imported ? "" : "required"}></label>
    ${groupSelect(list.data.groups, t?.group_id)}
    ${imported ? `<label><span>分享链接</span><textarea name="uri" rows="5" maxlength="8192" class="mono" placeholder="ss://… 或 vless://…?security=reality…" required>${esc(t?.uri)}</textarea></label><p class="hint">支持 SS、SS2022 和 VLESS Reality，地址需为 IPv4 或域名。</p>` :
      `<label><span>IPv4 或域名</span><input name="host" value="${esc(t?.host)}" required></label><label><span>端口</span><input name="port" type="number" min="1" max="65535" value="${t?.port || ""}" required></label>`}
    ${t?.configs.length ? `<p class="t-yellow">修改会影响 ${t.configs.length} 个引用配置；未启用远程唤醒或唤醒失败的被动节点需要手动同步。</p>` : ""}
    <div class="form-error"></div><div class="form-actions"><button type="button" data-action="modal-close">取消</button><button class="primary">保存</button></div></form>`);
}

Object.assign(FORMS, {
  async target(form, data) {
    const id = form.dataset.id;
    const body = {name: data.name, group_id: groupId(data)};
    if (form.dataset.imported === "true") body.uri = data.uri;
    else Object.assign(body, {host: data.host, port: Number(data.port)});
    await api(id ? "PATCH" : "POST", id ? `targets/${id}` : "targets", body);
    closeModal(); toast("已保存，可在配置中选择此目标"); refresh();
  },
  async route(form, data) {
    const id = form.dataset.id;
    const body = {name: data.name, group_id: groupId(data), node_ids: [...form.querySelectorAll("[name=route-node]")].map(s => Number(s.value))};
    const r = await api(id ? "PATCH" : "POST", id ? `routes/${id}` : "routes", body);
    closeModal(); toast(r.affected ? `已保存，自动更新 ${r.affected} 个引用配置` : "链路已保存"); refresh();
  },

});
Object.assign(ACTIONS, {
  "route-new": () => routeForm(),
  "route-edit": el => routeForm(list.data.routes.find(r => r.id === Number(el.dataset.id))),
  async "route-delete"(el) {
    const r = list.data.routes.find(r => r.id === Number(el.dataset.id));
    if (r.configs.length) return toast("此链路仍被配置引用，请先解除引用", true);
    if (!(await confirmBox("删除链路", `删除 <b>${esc(r.name)}</b>？`, "删除", true))) return;
    await api("DELETE", `routes/${r.id}`); toast("已删除"); refresh();
  },
  "target-new": () => targetForm(),
  "target-import": () => targetForm(null, true),
  "target-edit": el => targetForm(list.data.targets.find(t => t.id === Number(el.dataset.id))),
  async "target-delete"(el) {
    const t = list.data.targets.find(t => t.id === Number(el.dataset.id));
    if (t.configs.length) return toast("目标仍被配置引用，请先解除引用", true);
    if (!(await confirmBox("删除目标", `删除 <b>${esc(t.name)}</b> 的记录？`, "删除", true))) return;
    await api("DELETE", `targets/${t.id}`); toast("已删除"); refresh();
  },
  connection(el) {
    const c = chainById(el.dataset.id), x = c.connection;
    openModal(`<h2>${esc(c.remark)} · 连接信息</h2><div class="kv-grid"><span>协议</span><span>${esc(PROTOCOLS[x.protocol] || x.protocol)}</span><span>地址</span><code>${esc(x.host)}:${x.port}</code>${x.username ? `<span>用户名</span><code>${esc(x.username)}</code>` : ""}${x.password ? `<span>密码</span><code>${esc(x.password)}</code>` : ""}</div>
      <label><span>连接链接</span><textarea readonly rows="4">${esc(x.uri)}</textarea></label>
      ${x.tcp_only ? `<p class="hint">此 SOCKS5 链路仅支持 TCP，链接导入支持取决于客户端。</p>` : ""}
      <p class="hint">${chainStatus(c) === "saved" ? "外部目标连接信息已保存；尚未进行连通性检测。" : chainStatus(c) === "applied" ? "配置已应用；尚未进行连通性检测。" : "配置尚未全部应用，请等待节点确认。"}</p>
      <div class="form-actions"><button data-action="modal-close">关闭</button><button class="primary" data-action="copy" data-text="${esc(x.uri)}">复制链接</button></div>`);
  }
});

// ------------------------------------------------------------ 启动

if (location.protocol === "http:" && !["localhost", "127.0.0.1", "[::1]"].includes(location.hostname)) {
  $("#http-warning").hidden = false;
}
document.querySelectorAll(".logo").forEach((el) => (el.innerHTML = LOGO));
window.addEventListener("hashchange", route);
if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', route, {once:true});
else route();
