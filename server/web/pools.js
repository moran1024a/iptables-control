// Templates provide membership; every link owns its selection and preferences.
const LINK_STATES = {draft:'草稿', active:'已应用', pending:'等待应用', preparing:'准备下游', switching:'切换入口', waiting:'等待确认', error:'异常', deleting:'删除中'};
let poolView = null, linkEditor = null, templateEditor = null, instanceEditor = null;
const resourceName = (items, id, fallback='已删除') => items.find(x => x.id === id)?.name || fallback;
const memberName = (id) => id === 0 ? '直连下一阶段' : resourceName(poolView.nodes, id);
const linkState = l => `<span class="badge ${['error','waiting'].includes(l.state) ? 'removing' : l.state === 'active' ? 'applied' : 'applying'}">${LINK_STATES[l.state] || esc(l.state)}</span>`;
const poolHead = (title, button='') => `<div class="page-head"><h1>${title}</h1><div class="head-actions">${button}</div></div>`;

async function poolList(request) {
  const d = await api('GET','pools'); if (request !== viewRequest) return; poolView = d;
  render(poolHead('池', '<button class="primary" data-action="pool-new">新增池</button>') +
    '<p class="hint">成员增删同步到引用链路；优先级仅用于新实例初始化。池只接受已确认的主动节点。</p>' +
    (d.pools.map(p => `<div class="card" data-key="pool-${p.id}"><div class="card-head"><div class="title">${esc(p.name)}</div><div class="actions"><button class="small" data-action="pool-edit" data-id="${p.id}">编辑</button><button class="small danger" data-action="pool-delete" data-id="${p.id}" ${p.references.length ? 'disabled title="请先解除链路引用"' : ''}>删除</button></div></div><div class="path">${p.members.map((n,i) => `<span>${i+1}. ${esc(memberName(n))}</span>`).join('<span class="arrow">→</span>') || '暂无成员'}</div><div class="sub">${p.references.length ? '引用链路：'+p.references.map(esc).join('、') : '尚未被引用'}</div></div>`).join('') || emptyState('◉','先创建一个池，再将它添加到链路。')));
}

function poolForm(p) {
  templateEditor = {id:p?.id, name:p?.name || '', members:[...(p?.members || [])]}; drawPoolForm();
}
function drawPoolForm() {
  const p = templateEditor;
  openModal(`<form data-form="pool-save"><h2>${p.id ? '编辑池' : '新增池'}</h2><label><span>名称</span><input name="name" value="${esc(p.name)}" required maxlength="100"></label><p class="hint">从上到下为新实例的初始优先级。已有实例保持自己的顺序。</p><div class="member-list">${p.members.map((n,i) => `<div class="member-row"><span>${i+1}. ${esc(memberName(n))}</span><button type="button" class="small" data-action="pool-up" data-index="${i}" ${i ? '' : 'disabled'}>上移</button><button type="button" class="small danger" data-action="pool-remove" data-index="${i}">移除</button></div>`).join('')}</div><div class="inline-fields"><select id="pool-add-node"><option value="">选择主动节点</option>${poolView.nodes.filter(n => n.eligible && !p.members.includes(n.id)).map(n => `<option value="${n.id}">${esc(n.name)}</option>`).join('')}</select><button type="button" data-action="pool-add">添加成员</button></div><div class="form-error"></div><div class="form-actions"><button type="button" data-action="modal-close">取消</button><button class="primary">保存</button></div></form>`);
}
function rememberPool() { templateEditor.name = $('#modal [name=name]').value; }

async function linkList(request) {
  const d = await api('GET','links'); if (request !== viewRequest) return; poolView = d;
  render(poolHead('链路', '<button class="primary" data-action="link-new">新增链路</button>') + '<p class="hint">每个链路组拥有固定入口和目标，以及独立的池优先级。当前路径持续失联 60 秒后按优先级灾备，恢复后不自动切回。</p>' +
    (d.links.map(l => `<a href="#/routes/${l.id}" class="card link-card" data-key="link-${l.id}"><div class="card-head"><div class="title">${esc(l.name)}</div>${linkState(l)}</div><div class="sub">${l.plan.entry ? esc(resourceName(d.nodes,l.plan.entry)) : '直达目标'} → ${esc(resourceName(d.targets,l.plan.target,'未选择目标'))}</div><div class="path">${l.hops.length ? l.hops.map(h => esc(h.name)).join(' → ') + ' → ' : ''}${esc(resourceName(d.targets,l.plan.target,'草稿'))}</div>${l.error ? `<div class="hint t-red">${esc(l.error)}</div>` : ''}${l.cleanup.length ? `<div class="hint">${l.cleanup.length} 项旧规则等待清理</div>` : ''}</a>`).join('') || emptyState('⇄','创建链路，选择入口、池和目标。')));
}

function topology(l) {
  const d = poolView, plan = l.plan;
  const cards = [];
  if (plan.entry) cards.push(`<div class="topology-node"><small>固定入口</small><a href="#/nodes/${plan.entry}">${esc(resourceName(d.nodes,plan.entry))}</a><code>${esc(plan.port || '自动端口')}</code></div>`);
  for (const st of plan.stages) {
    if (st.kind === 'node') { cards.push(`<div class="topology-node"><small>固定节点</small><a href="#/nodes/${st.node_id}">${esc(memberName(st.node_id))}</a></div>`); continue; }
    const current = l.selected[st.id], next = l.job?.selected[st.id];
    cards.push(`<button class="topology-node topology-pool" data-action="instance-edit" data-stage="${esc(st.id)}" ${l.job || l.state === 'deleting' ? 'disabled' : ''}><small>池 · ${esc(resourceName(d.pools,st.pool_id))}</small><strong>${current == null ? '尚未应用' : esc(memberName(current))}</strong>${next != null && next !== current ? `<span class="t-yellow">待切换：${esc(memberName(next))}</span>` : ''}<span>${st.order.length} 个候选${st.locked != null ? ' · 已锁定' : ''}</span>${st.direct ? '<small>含直连路径</small>' : ''}</button>`);
  }
  cards.push(`<div class="topology-node"><small>目标</small><strong>${esc(resourceName(d.targets,plan.target,'未选择'))}</strong></div>`);
  return `<div class="topology" aria-label="链路拓扑">${cards.join('<span class="topology-arrow" aria-hidden="true">→</span>')}</div>`;
}

async function linkDetail(id, request) {
  const d = await api('GET',`links/${id}`); if (request !== viewRequest) return; poolView=d; const l=d.link;
  render(`<a class="back" href="#/routes">← 链路</a>` + poolHead(esc(l.name), `${linkState(l)}<button data-action="link-edit" ${l.job || l.state === 'deleting' ? 'disabled' : ''}>编辑链路</button><button class="danger" data-action="link-delete" ${l.job || l.state === 'deleting' ? 'disabled' : ''}>删除</button>`) +
    `<div class="panel">${topology(l)}<p class="hint">点击池设置独立优先级、锁定、禁用或手动切换。直连只跳过对应的池。</p>${l.error ? `<p class="t-red">${esc(l.error)}</p><button data-action="link-retry">重试同步</button>` : ''}${l.cleanup.length ? `<p class="hint">旧规则待清理：${l.cleanup.map(r=>`${esc(memberName(r.node_id))}:${r.port}`).join('、')}。这些成员暂不能移除。</p>` : ''}</div>` +
    `<div class="panel"><h2>当前连接</h2>${l.connection ? `<div class="entry"><code>${esc(l.connection.host)}:${l.connection.port}</code><button class="small" data-action="copy" data-text="${esc(l.connection.uri)}">复制${l.connection.protocol==='endpoint' ? '地址' : '导入链接'}</button></div>` : '<p class="muted">尚未确认生效</p>'}<p class="hint">切换以节点回执为准，可能短暂断线。质量指标仅供参考，不自动择优。</p><a href="#/quality/links/${l.id}">网络质量与趋势 →</a></div>` +
    `<div class="panel"><h2>切换记录</h2>${l.events.length ? `<table><tbody>${l.events.map(e=>`<tr><td>${esc(fmtTime(e.ts))}</td><td>${esc(e.message)}</td></tr>`).join('')}</tbody></table>` : '<p class="muted">暂无记录</p>'}</div>`);
}

function linkForm(l) {
  linkEditor = {id:l?.id, revision:l?.revision, name:l?.name || '', plan:structuredClone(l?.plan || {entry:null,port:null,target:null,stages:[]})};
  drawLinkForm();
}
function rememberLink() {
  const f=$('#modal form'); if(!f || f.dataset.form!=='link-save') return;
  linkEditor.name=f.elements.name.value; linkEditor.plan.entry=Number(f.elements.entry.value)||null;
  linkEditor.plan.target=Number(f.elements.target.value)||null; linkEditor.plan.port=Number(f.elements.port.value)||null;
}
function drawLinkForm() {
  const p=linkEditor.plan;
  const options=(items,selected)=>items.map(x=>`<option value="${x.id}" ${x.id===selected?'selected':''}>${esc(x.name)}</option>`).join('');
  openModal(`<form data-form="link-save"><h2>${linkEditor.id?'编辑链路':'新增链路'}</h2><label><span>名称</span><input name="name" value="${esc(linkEditor.name)}" required></label><div class="inline-fields"><label><span>入口节点</span><select name="entry"><option value="">无入口（直达目标）</option>${options(poolView.nodes,p.entry)}</select></label><label><span>入口端口</span><input name="port" type="number" min="1" max="65535" value="${p.port||''}" placeholder="自动分配"></label></div><h3>中间路径</h3><div class="member-list">${p.stages.map((st,i)=>`<div class="member-row"><span>${i+1}. ${st.kind==='pool'?'池 · '+esc(resourceName(poolView.pools,st.pool_id)):esc(memberName(st.node_id))}</span><button type="button" class="small" data-action="stage-up" data-index="${i}" ${i?'':'disabled'}>上移</button><button type="button" class="small danger" data-action="stage-remove" data-index="${i}">移除</button></div>`).join('') || '<p class="muted">入口直接连接目标</p>'}</div><div class="inline-fields"><select id="stage-add"><option value="">选择池或固定节点</option><optgroup label="池">${poolView.pools.map(x=>`<option value="p:${x.id}">${esc(x.name)}</option>`).join('')}</optgroup><optgroup label="固定节点">${poolView.nodes.map(x=>`<option value="n:${x.id}">${esc(x.name)}</option>`).join('')}</optgroup></select><button type="button" data-action="stage-add">添加阶段</button></div><label><span>目标</span><select name="target"><option value="">未选择</option>${options(poolView.targets,p.target)}</select></label><p class="hint">池按模板初始化。保存后点击链路中的池，可设置优先级及直连。被动节点参与池切换需要 RC4 Agent；未开启唤醒时需要手动同步。</p><div class="form-error"></div><div class="form-actions"><button type="button" data-action="modal-close">取消</button>${!linkEditor.id || poolView.link?.state==='draft'?'<button name="draft" value="1">保存草稿</button>':''}<button class="primary">保存并应用</button></div></form>`);
}

function instanceForm(sid) {
  const l=poolView.link; instanceEditor={link:structuredClone(l),stage:sid}; drawInstance();
}
function instance() { return instanceEditor.link.plan.stages.find(s=>s.id===instanceEditor.stage); }
function drawInstance() {
  const s=instance(),l=instanceEditor.link;
  openModal(`<form data-form="instance-save"><h2>${esc(resourceName(poolView.pools,s.pool_id))} · 本链路设置</h2><p class="hint">默认节点即第一优先级。锁定后停止自动灾备；当前使用的成员需先切换才能禁用。</p><div class="member-list">${s.order.map((n,i)=>`<div class="member-row"><span>${i+1}. ${esc(memberName(n))}${l.selected[s.id]===n?' <b class="t-green">当前</b>':''}${i===0?' <small>默认</small>':''}</span><button type="button" class="small" data-action="instance-up" data-index="${i}" ${i?'':'disabled'}>上移</button><button type="button" class="small" data-action="instance-default" data-index="${i}" ${i?'':'disabled'}>设为默认</button><button type="button" class="small" data-action="instance-disable" data-member="${n}" ${l.selected[s.id]===n?'disabled':''}>${s.disabled.includes(n)?'启用':'禁用'}</button><button type="button" class="small" data-action="instance-switch" data-member="${n}" ${l.selected[s.id]===n || s.disabled.includes(n) || (s.locked!=null&&s.locked!==n)?'disabled':''}>切换</button></div>`).join('')}</div><label><span>锁定成员</span><select id="instance-lock"><option value="">不锁定（允许灾备）</option>${s.order.filter(n=>!s.disabled.includes(n)).map(n=>`<option value="${n}" ${s.locked===n?'selected':''}>${esc(memberName(n))}</option>`).join('')}</select></label><button type="button" data-action="instance-direct" ${l.selected[s.id]===0 || s.locked===0 ? 'disabled' : ''}>${s.direct?'移除直连规则':'新增直连规则'}</button><div class="form-error"></div><div class="form-actions"><button type="button" data-action="modal-close">取消</button><button class="primary">保存并应用</button></div></form>`);
}
function rememberInstance() { const v=$('#instance-lock').value; instance().locked=v===''?null:Number(v); }

async function configSummary(request) {
  const d=await api('GET','configs'); if(request!==viewRequest)return;
  render(poolHead('配置','<a class="button" href="#/routes">管理链路 →</a>')+'<p class="hint">连接信息由链路生成。编辑入口、目标和路径，请前往对应链路。</p>'+ (d.chains.length?`<div class="panel table-scroll"><table><thead><tr><th>名称</th><th>入口地址</th><th>状态</th><th>连接信息</th></tr></thead><tbody>${d.chains.map(c=>`<tr data-key="summary-${c.id}"><td><a href="#/routes/${c.link_id||''}">${esc(c.remark)}</a></td><td><code>${c.connection?esc(c.connection.host)+':'+c.connection.port:'等待入口确认'}</code></td><td>${c.link_state?linkState({state:c.link_state}):esc(c.status)}${c.link_error?`<small class="t-red">${esc(c.link_error)}</small>`:''}</td><td>${c.connection?`<button class="small" data-action="copy" data-text="${esc(c.connection.uri)}">复制${c.connection.protocol==='endpoint'?'地址':'导入链接'}</button>`:''}</td></tr>`).join('')}</tbody></table></div>`:emptyState('⇄','尚无配置，请先创建链路。')));
}

Object.assign(FORMS, {
  async 'pool-save'(form,data) { await api(templateEditor.id?'PATCH':'POST',templateEditor.id?`pools/${templateEditor.id}`:'pools',{name:data.name,members:templateEditor.members});closeModal();refresh(); },
  async 'link-save'(form) { rememberLink(); const body={...linkEditor,apply:form.dataset.draft!=='1'};delete form.dataset.draft;const r=await api(body.id?'PATCH':'POST',body.id?`links/${body.id}`:'links',body);closeModal();location.hash=`#/routes/${r.id}`;refresh(); },
  async 'instance-save'() { rememberInstance();const l=instanceEditor.link;await api('PATCH',`links/${l.id}`,{name:l.name,revision:l.revision,plan:l.plan,apply:true});closeModal();refresh(); }
});
document.addEventListener('submit',e=>{if(e.target.dataset.form==='link-save')e.target.dataset.draft=e.submitter?.name==='draft'?'1':'0';},true);
Object.assign(ACTIONS, {
  'pool-new':()=>poolForm(), 'pool-edit':el=>poolForm(poolView.pools.find(p=>p.id===Number(el.dataset.id))),
  async 'pool-delete'(el){if(!confirm('删除此池？'))return;await api('DELETE',`pools/${el.dataset.id}`);refresh();},
  'pool-up'(el){rememberPool();const i=Number(el.dataset.index),a=templateEditor.members;[a[i-1],a[i]]=[a[i],a[i-1]];drawPoolForm();},
  'pool-remove'(el){rememberPool();templateEditor.members.splice(Number(el.dataset.index),1);drawPoolForm();},
  'pool-add'(){rememberPool();const n=Number($('#pool-add-node').value);if(n)templateEditor.members.push(n);drawPoolForm();},
  'link-new':()=>linkForm(), 'link-edit':()=>linkForm(poolView.link),
  async 'link-delete'(){if(!confirm('删除链路并清理其转发规则？'))return;await api('DELETE',`links/${poolView.link.id}`);location.hash='#/routes';},
  async 'link-retry'(){const l=poolView.link;await api('POST',`links/${l.id}/action`,{action:'retry',revision:l.revision});refresh();},
  'stage-add'(){rememberLink();const [kind,id]=$('#stage-add').value.split(':');if(!id)return;linkEditor.plan.stages.push({id:crypto.randomUUID?.()||String(Date.now()),kind:kind==='p'?'pool':'node',...(kind==='p'?{pool_id:Number(id)}:{node_id:Number(id)})});drawLinkForm();},
  'stage-up'(el){rememberLink();const i=Number(el.dataset.index),a=linkEditor.plan.stages;[a[i-1],a[i]]=[a[i],a[i-1]];drawLinkForm();},
  'stage-remove'(el){rememberLink();linkEditor.plan.stages.splice(Number(el.dataset.index),1);drawLinkForm();},
  'instance-edit':el=>instanceForm(el.dataset.stage),
  'instance-up'(el){rememberInstance();const i=Number(el.dataset.index),a=instance().order;[a[i-1],a[i]]=[a[i],a[i-1]];drawInstance();},
  'instance-default'(el){rememberInstance();const a=instance().order; a.unshift(a.splice(Number(el.dataset.index),1)[0]);drawInstance();},
  'instance-disable'(el){rememberInstance();const n=Number(el.dataset.member),s=instance();s.disabled=s.disabled.includes(n)?s.disabled.filter(x=>x!==n):[...s.disabled,n];if(s.locked===n)s.locked=null;drawInstance();},
  'instance-direct'(){rememberInstance();const s=instance();s.direct=!s.direct;s.order=s.direct?[...s.order,0]:s.order.filter(n=>n!==0);s.disabled=s.disabled.filter(n=>n!==0);drawInstance();},
  async 'instance-switch'(el){const l=instanceEditor.link;await api('POST',`links/${l.id}/action`,{action:'switch',revision:l.revision,stage:instanceEditor.stage,member:Number(el.dataset.member)});closeModal();refresh();}
});
