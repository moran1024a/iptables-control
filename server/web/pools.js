// Templates provide membership; every link owns its selection and preferences.
const LINK_STATES = {draft:'草稿', active:'已应用', pending:'等待应用', preparing:'准备下游', switching:'切换入口', waiting:'等待确认', error:'异常', deleting:'删除中'};
let poolView = null, linkEditor = null, templateEditor = null, instanceEditor = null, quickSwitch = null;
const resourceName = (items, id, fallback='已删除') => items.find(x => x.id === id)?.name || fallback;
const memberName = (id) => id === 0 ? 'direct' : resourceName(poolView.nodes, id);
const linkState = l => `<span class="badge ${['error','waiting'].includes(l.state) ? 'removing' : l.state === 'active' ? 'applied' : 'applying'}">${LINK_STATES[l.state] || esc(l.state)}</span>`;
const poolHead = (title, button='') => `<div class="page-head"><h1>${title}</h1><div class="head-actions">${button}</div></div>`;

async function poolList(request) {
  const d = await api('GET','pools'); if (request !== viewRequest) return; poolView = d;
  render(poolHead('池', '<button class="primary" data-action="pool-new">新增池</button>') +
    (d.pools.map(p => `<div class="card" data-key="pool-${p.id}"><div class="card-head"><div class="title">${esc(p.name)}</div><div class="actions"><button class="small" data-action="pool-edit" data-id="${p.id}">编辑</button><button class="small danger" data-action="pool-delete" data-id="${p.id}" ${p.references.length ? 'disabled title="请先解除链路引用"' : ''}>删除</button></div></div><div class="path">${p.members.map((n,i) => `<span>${i+1}. ${esc(memberName(n))}</span>`).join('<span class="arrow">→</span>') || '暂无成员'}</div><div class="sub">${p.references.length ? '引用链路：'+p.references.map(esc).join('、') : '尚未被引用'}</div></div>`).join('') || emptyState('◉','先创建一个池，再将它添加到链路。')));
}

function poolForm(p) {
  templateEditor = {id:p?.id, name:p?.name || '', members:[...(p?.members || [])]}; drawPoolForm();
}
function drawPoolForm() {
  const p = templateEditor;
  openModal(`<form data-form="pool-save"><h2>${p.id ? '编辑池' : '新增池'}</h2><label><span>名称</span><input name="name" value="${esc(p.name)}" required maxlength="100"></label><div class="member-list">${p.members.map((n,i) => `<div class="member-row"><span>${i+1}. ${esc(memberName(n))}</span><button type="button" class="small" data-action="pool-up" data-index="${i}" ${i ? '' : 'disabled'}>上移</button><button type="button" class="small danger" data-action="pool-remove" data-index="${i}">移除</button></div>`).join('')}</div><div class="inline-fields"><select id="pool-add-node"><option value="">选择主动节点</option>${poolView.nodes.filter(n => n.eligible && !p.members.includes(n.id)).map(n => `<option value="${n.id}">${esc(n.name)}</option>`).join('')}</select><button type="button" data-action="pool-add">添加成员</button></div><div class="form-error"></div><div class="form-actions"><button type="button" data-action="modal-close">取消</button><button class="primary">保存</button></div></form>`);
}
function rememberPool() { templateEditor.name = $('#modal [name=name]').value; }

async function linkList(request) {
  const d = await api('GET','links'); if (request !== viewRequest) return; poolView = d;
  render(poolHead('链路', '<button class="primary" data-action="link-new">新增链路</button>') +
    (d.links.map(l => `<a href="#/routes/${l.id}" class="card link-card" data-key="link-${l.id}"><div class="card-head"><div class="title">${esc(l.name)}</div>${linkState(l)}</div><div class="sub">${l.plan.entry ? esc(resourceName(d.nodes,l.plan.entry)) : '直达目标'} → ${esc(resourceName(d.targets,l.plan.target,'未选择目标'))}</div><div class="path">${l.hops.length ? l.hops.map(h => esc(h.name)).join(' → ') + ' → ' : ''}${esc(resourceName(d.targets,l.plan.target,'草稿'))}</div>${l.error ? `<div class="hint t-red">${esc(l.error)}</div>` : ''}${l.cleanup.length ? `<div class="hint">${l.cleanup.length} 项旧规则等待清理</div>` : ''}</a>`).join('') || emptyState('⇄','创建链路，选择入口、池和目标。')));
}

function switchBlocked(l, stage, member) {
  if (l.job) return '已有切换正在进行，请等待确认';
  if (l.state === 'deleting') return '链路正在删除';
  if (!l.chain_id) return '请先保存并应用链路';
  if (stage.disabled.includes(member)) return '此成员已禁用';
  if (stage.locked != null && stage.locked !== member) return '请先在池设置中解除锁定';
  const node = poolView.nodes.find(n => n.id === member);
  if (member !== 0 && (!node?.eligible || node.status !== 'synced')) return node?.status === 'offline' ? '节点离线，暂不能切换' : '节点尚未就绪，请检查节点状态';
  if (l.selected[stage.id] === member && stage.default === member) return '当前默认节点已生效';
  return '';
}

function topology(l) {
  const d = poolView, plan = l.plan;
  // Fixed dimensions keep HTML cards and SVG cables aligned without layout observers.
  const cardH = 76, gap = 12, inset = 14, headerH = 56, gutter = 76;
  const columns = [];
  const fixed = (kind, id, label) => ({kind, id, label, width:204, height:104});
  if (plan.entry) columns.push(fixed('entry', plan.entry, '入口'));
  for (const stage of plan.stages) {
    columns.push(stage.kind === 'pool'
      ? {kind:'pool', stage, width:232, height:headerH + inset*2 + Math.max(1,stage.order.length)*(cardH+gap)-gap+2}
      : fixed('node', stage.node_id, '节点'));
  }
  columns.push(fixed('target', plan.target, '目标'));
  const height = Math.max(...columns.map(c=>c.height)) + 48;
  let x = 24;
  for (const [index, col] of columns.entries()) {
    col.x=x; col.y=(height-col.height)/2;
    const members = col.kind === 'pool' ? col.stage.order : [col.id];
    col.points = members.map((member,i)=>({member,key:`${index}:${i}`,x:x+(col.kind==='pool'?inset+1:0),
      y:col.kind==='pool'?col.y+1+headerH+inset+i*(cardH+gap)+cardH/2:height/2,
      width:col.kind==='pool'?col.width-(inset+1)*2:col.width}));
    x+=col.width+gutter;
  }
  const width=x-gutter+24;
  const picked=(col,selected)=>col.kind==='pool'?col.points.find(p=>p.member===selected[col.stage.id]):col.points[0];
  const current=columns.map(col=>picked(col,l.selected));
  const currentIds=current.slice(0,-1).filter(Boolean).map(p=>p.member).filter(Boolean);
  const matches=current.every(Boolean) && currentIds.join(',')===l.hops.map(h=>h.node_id).join(',')
    && plan.target===l.applied_target_id && (!plan.entry || plan.port===l.hops[0]?.port);
  const healthy=currentIds.every(id=>!['offline','error','skew','deleting','new'].includes(d.nodes.find(n=>n.id===id)?.status));
  const flowing=matches && healthy && (l.state==='active' || l.state==='preparing');
  const pending=l.job ? columns.map(col=>picked(col,l.job.selected)) : [];
  const cables=[], active=[], waiting=[];
  const pathBetween=(a,b)=>{
    const x=a.x+a.width,bend=Math.min(32,(b.x-x)/4);
    return `M ${x} ${a.y} C ${x+bend} ${a.y} ${b.x-bend} ${b.y} ${b.x} ${b.y}`;
  };
  for(let i=0;i<columns.length-1;i++) {
    const left=columns[i],right=columns[i+1];
    for(const a of left.points) for(const b of right.points) {
      if(a.member && a.member===b.member && right.kind!=='target') continue;
      cables.push(`<path class="topology-candidate" data-from="${a.key}" data-to="${b.key}" d="${pathBetween(a,b)}"/>`);
    }
    if(matches) {
      const path=pathBetween(current[i],current[i+1]);
      active.push(`<path class="topology-confirmed${flowing?' flowing':''}" d="${path}"/>`);
      if(flowing) active.push(`<path class="topology-flow" d="${path}"/>`);
    }
    if(pending[i] && pending[i+1]) waiting.push(`<path class="topology-pending" d="${pathBetween(pending[i],pending[i+1])}"/>`);
  }
  const cards=columns.map((col,index)=>{
    if(col.kind!=='pool') {
      const node=d.nodes.find(n=>n.id===col.id),target=d.targets.find(t=>t.id===col.id);
      const name=col.kind==='target'?(target?.name||'未选择目标'):(node?.name||'已删除节点');
      const subtitle=col.kind==='entry'?`${node?.addr||''}:${plan.port||'自动端口'}`:col.kind==='target'?'':node?.addr||'';
      return `<div class="topology-fixed${flowing?' on-route':''}" data-key="fixed-${index}-${col.id}" style="left:${col.x}px;top:${col.y}px;width:${col.width}px"><small>${col.label}${col.kind!=='target'?` · ${node?.mode==='passive'?'被动':'主动'}`:''}</small>${col.kind==='target'?`<strong title="${esc(name)}">${esc(name)}</strong>`:`<a href="#/nodes/${col.id}" title="${esc(name)}">${esc(name)}</a>`}<span class="topology-address" title="${esc(subtitle)}">${esc(subtitle)}</span></div>`;
    }
    const st=col.stage;
    return `<section class="topology-pool" data-key="stage-${esc(st.id)}" aria-label="${esc(resourceName(d.pools,st.pool_id))}" style="left:${col.x}px;top:${col.y}px;width:${col.width}px;height:${col.height}px"><div class="topology-pool-head"><div><small>池 · ${st.order.length} 个候选${st.locked!=null?' · 已锁定':''}</small><strong title="${esc(resourceName(d.pools,st.pool_id))}">${esc(resourceName(d.pools,st.pool_id))}</strong></div><button class="small" data-action="instance-edit" data-stage="${esc(st.id)}" ${l.job||l.state==='deleting'?'disabled':''}>设置</button></div><div class="topology-members">${col.points.map((p,i)=>{
      const node=d.nodes.find(n=>n.id===p.member),isCurrent=matches&&l.selected[st.id]===p.member,isPending=l.job?.selected[st.id]===p.member;
      const reason=switchBlocked(l,st,p.member),disabled=st.disabled.includes(p.member);
      const status=p.member===0?'':({synced:'在线',offline:'离线',syncing:'等待同步',new:'未连接',error:'异常',skew:'时间异常',deleting:'删除中'}[node?.status]||'未就绪');
      const label=isPending?'待确认':isCurrent?(flowing?'生效':'上次确认'):disabled?'禁用':'';
      return `<button class="topology-choice${isCurrent?' is-current':''}${isCurrent&&flowing?' on-route':''}${isPending?' is-pending':''}${disabled?' is-disabled':''}" data-key="member-${p.member}" data-point="${p.key}" data-action="topology-switch" data-stage="${esc(st.id)}" data-member="${p.member}" title="${esc(reason||'切换到 '+memberName(p.member))}" ${reason?'disabled':''}><span class="topology-choice-title"><span class="topology-rank">${i+1}</span><strong title="${esc(memberName(p.member))}">${esc(p.member===0?'direct':node?.name||'已删除节点')}</strong>${st.default===p.member?'<small class="topology-default">默认</small>':''}</span><span class="topology-choice-meta"><span class="${p.member===0?'':'topology-node-status'} ${node?.status==='synced'||p.member===0?'ready':''}">${esc(status)}</span><span class="topology-state">${esc(label)}${st.locked===p.member?' · 锁定':''}</span></span></button>`;
    }).join('')||'<p class="hint">暂无成员，请打开设置</p>'}</div></section>`;
  }).join('');
  return `<div class="topology-toolbar"><div class="topology-legend"><span class="legend-active">生效</span><span class="legend-pending">待确认</span></div><div class="topology-tools"><button class="small" data-action="canvas-out" title="缩小" aria-label="缩小">−</button><span data-zoom-value>100%</span><button class="small" data-action="canvas-in" title="放大" aria-label="放大">+</button><button class="small" data-action="canvas-fit">适应画布</button></div></div><div class="topology" data-key="canvas-${l.id}" tabindex="0" role="region" aria-label="链路画布，拖动平移，滚轮缩放"><div class="topology-canvas" style="width:${width}px;height:${height}px">${cards}<svg class="topology-cables" width="${width}" height="${height}" viewBox="0 0 ${width} ${height}" aria-hidden="true">${cables.join('')}${active.join('')}${waiting.join('')}</svg></div></div>`;
}

function quickSwitchForm(l, sid, member) {
  const stage=l.plan.stages.find(s=>s.id===sid && s.kind==='pool');
  if(!stage) return;
  const reason=switchBlocked(l,stage,member);
  if(reason) return toast(reason,true);
  quickSwitch={id:l.id,revision:l.revision,stage:sid,member};
  openModal(`<form data-form="quick-switch"><h2>切换节点</h2><p>切换到 <strong>${esc(memberName(member))}</strong>，并设为默认节点？</p><div class="form-error" role="alert"></div><div class="form-actions"><button type="button" data-action="modal-close">取消</button><button class="primary">确认切换</button></div></form>`);
}

function focusTopology(event) {
  const member=event.target.closest?.('.topology-choice');
  if(!member || member.contains(event.relatedTarget)) return;
  const enabled=event.type==='mouseover'||event.type==='focusin';
  member.closest('.topology-canvas').querySelectorAll('.topology-candidate').forEach(path=>{
    if(path.dataset.from===member.dataset.point || path.dataset.to===member.dataset.point) path.classList.toggle('is-focused',enabled);
  });
}
for(const event of ['mouseover','mouseout','focusin','focusout']) document.addEventListener(event,focusTopology);

const topologyViews = new Map();
let topologyBinding = null;

function releaseTopology() {
  topologyBinding?.observer.disconnect();
  topologyBinding = null;
}

function mountTopology(id) {
  const viewport = app.querySelector('.topology');
  if (!viewport) return;
  const canvas = viewport.querySelector('.topology-canvas');
  let view = topologyViews.get(id);
  if (!view) { view = {x:0,y:0,scale:1,auto:true}; topologyViews.set(id,view); }
  const fitScale = () => Math.min(1,(viewport.clientWidth-24)/canvas.offsetWidth,(viewport.clientHeight-24)/canvas.offsetHeight);
  const apply = () => {
    canvas.style.transform = `translate(${view.x}px,${view.y}px) scale(${view.scale})`;
    app.querySelector('[data-zoom-value]').textContent = `${Math.round(view.scale*100)}%`;
  };
  const fit = () => {
    view.auto = true; view.scale = fitScale();
    view.x = (viewport.clientWidth-canvas.offsetWidth*view.scale)/2;
    view.y = (viewport.clientHeight-canvas.offsetHeight*view.scale)/2;
    apply();
  };
  const zoom = (factor,x=viewport.clientWidth/2,y=viewport.clientHeight/2) => {
    const scale = Math.max(Math.min(.1,fitScale()),Math.min(3,view.scale*factor));
    const ratio = scale/view.scale;
    view.x = x-(x-view.x)*ratio; view.y = y-(y-view.y)*ratio;
    view.scale = scale; view.auto = false; apply();
  };
  if (topologyBinding?.viewport !== viewport) {
    releaseTopology();
    const points = new Map();
    const position = e => { const r=viewport.getBoundingClientRect();return {x:e.clientX-r.left-viewport.clientLeft,y:e.clientY-r.top-viewport.clientTop}; };
    const midpoint = values => ({x:values.reduce((a,p)=>a+p.x,0)/values.length,y:values.reduce((a,p)=>a+p.y,0)/values.length});
    const distance = values => Math.hypot(values[0].x-values[1].x,values[0].y-values[1].y);
    viewport.addEventListener('wheel',e=>{e.preventDefault();const p=position(e);zoom(Math.exp(-Math.max(-300,Math.min(300,e.deltaY))*.002),p.x,p.y);},{passive:false});
    viewport.onpointerdown = e => {
      if (e.button!==0 || e.target.closest('button,a,input,select') || points.size>=2) return;
      e.preventDefault(); viewport.focus({preventScroll:true});
      points.set(e.pointerId,position(e)); viewport.setPointerCapture(e.pointerId);
      viewport.classList.add('is-dragging');
    };
    viewport.onpointermove = e => {
      if (!points.has(e.pointerId)) return;
      const before=[...points.values()]; points.set(e.pointerId,position(e)); const after=[...points.values()];
      const a=midpoint(before),b=midpoint(after);
      if (points.size===2 && distance(before)>1) zoom(distance(after)/distance(before),a.x,a.y);
      view.x+=b.x-a.x; view.y+=b.y-a.y; view.auto=false; apply();
    };
    const end = e => {points.delete(e.pointerId);if(!points.size)viewport.classList.remove('is-dragging');};
    viewport.onpointerup=end; viewport.onpointercancel=end; viewport.onlostpointercapture=end;
    viewport.onkeydown = e => {
      if (e.target!==viewport) return;
      if (e.key==='+' || e.key==='=') zoom(1.2);
      else if (e.key==='-') zoom(1/1.2);
      else if (e.key==='0') fit();
      else if (['ArrowLeft','ArrowRight','ArrowUp','ArrowDown'].includes(e.key)) {
        view.x+=e.key==='ArrowLeft'?30:e.key==='ArrowRight'?-30:0;
        view.y+=e.key==='ArrowUp'?30:e.key==='ArrowDown'?-30:0;
        view.auto=false;apply();
      } else return;
      e.preventDefault();
    };
    const observer=new ResizeObserver(()=>view.auto?fit():apply());
    observer.observe(viewport);
    topologyBinding={viewport,observer,fit,zoom};
  }
  if(view.auto) fit(); else apply();
}

async function linkDetail(id, request) {
  const d = await api('GET',`links/${id}`); if (request !== viewRequest) return; poolView=d; const l=d.link;
  render(`<a class="back" href="#/routes">← 链路</a>` + poolHead(esc(l.name), `${linkState(l)}<button data-action="link-edit" ${l.job || l.state === 'deleting' ? 'disabled' : ''}>编辑链路</button><button class="danger" data-action="link-delete" ${l.job || l.state === 'deleting' ? 'disabled' : ''}>删除</button>`) +
    `<div class="panel">${topology(l)}${l.error ? `<p class="t-red">${esc(l.error)}</p><button data-action="link-retry">重试同步</button>` : ''}${l.cleanup.length ? `<p class="hint">旧规则待清理：${l.cleanup.map(r=>`${esc(memberName(r.node_id))}:${r.port}`).join('、')}</p>` : ''}</div>` +
    `<div class="panel"><h2>当前连接</h2>${l.connection ? `<div class="entry"><code>${esc(l.connection.host)}:${l.connection.port}</code><button class="small" data-action="copy" data-text="${esc(l.connection.uri)}">复制${l.connection.protocol==='endpoint' ? '地址' : '导入链接'}</button></div>` : '<p class="muted">尚未确认生效</p>'}<a href="#/quality/links/${l.id}">网络质量 →</a></div>` +
    `<div class="panel"><h2>切换记录</h2>${l.events.length ? `<table><tbody>${l.events.map(e=>`<tr><td>${esc(fmtTime(e.ts))}</td><td>${esc(e.message)}</td></tr>`).join('')}</tbody></table>` : '<p class="muted">暂无记录</p>'}</div>`);
  mountTopology(id);
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
  openModal(`<form data-form="link-save"><h2>${linkEditor.id?'编辑链路':'新增链路'}</h2><label><span>名称</span><input name="name" value="${esc(linkEditor.name)}" required></label><div class="inline-fields"><label><span>入口节点</span><select name="entry"><option value="">无入口（直达目标）</option>${options(poolView.nodes,p.entry)}</select></label><label><span>入口端口</span><input name="port" type="number" min="1" max="65535" value="${p.port||''}" placeholder="自动分配"></label></div><h3>中间路径</h3><div class="member-list">${p.stages.map((st,i)=>`<div class="member-row"><span>${i+1}. ${st.kind==='pool'?'池 · '+esc(resourceName(poolView.pools,st.pool_id)):esc(memberName(st.node_id))}</span><button type="button" class="small" data-action="stage-up" data-index="${i}" ${i?'':'disabled'}>上移</button><button type="button" class="small danger" data-action="stage-remove" data-index="${i}">移除</button></div>`).join('') || '<p class="muted">入口直接连接目标</p>'}</div><div class="inline-fields"><select id="stage-add"><option value="">选择池或固定节点</option><optgroup label="池">${poolView.pools.map(x=>`<option value="p:${x.id}">${esc(x.name)}</option>`).join('')}</optgroup><optgroup label="固定节点">${poolView.nodes.map(x=>`<option value="n:${x.id}">${esc(x.name)}</option>`).join('')}</optgroup></select><button type="button" data-action="stage-add">添加阶段</button></div><label><span>目标</span><select name="target"><option value="">未选择</option>${options(poolView.targets,p.target)}</select></label><div class="form-error"></div><div class="form-actions"><button type="button" data-action="modal-close">取消</button>${!linkEditor.id || poolView.link?.state==='draft'?'<button name="draft" value="1">保存草稿</button>':''}<button class="primary">保存并应用</button></div></form>`);
}

function instanceForm(sid) {
  const l=poolView.link; instanceEditor={link:structuredClone(l),source:structuredClone(l),stage:sid}; drawInstance();
}
function instance() { return instanceEditor.link.plan.stages.find(s=>s.id===instanceEditor.stage); }
function drawInstance() {
  const s=instance(),l=instanceEditor.link;
  openModal(`<form data-form="instance-save"><h2>${esc(resourceName(poolView.pools,s.pool_id))}</h2><div class="member-list">${s.order.map((n,i)=>`<div class="member-row"><span>${i+1}. ${esc(memberName(n))}${l.selected[s.id]===n?' <b class="t-green">生效</b>':''}${s.default===n?' <small>默认</small>':''}</span><button type="button" class="small" data-action="instance-up" data-index="${i}" ${i?'':'disabled'} aria-label="提高优先级">↑</button><button type="button" class="small" data-action="instance-down" data-index="${i}" ${i<s.order.length-1?'':'disabled'} aria-label="降低优先级">↓</button><button type="button" class="small" data-action="instance-disable" data-member="${n}" ${l.selected[s.id]===n?'disabled':''}>${s.disabled.includes(n)?'启用':'禁用'}</button></div>`).join('')}</div><label><span>默认节点</span><select id="instance-default">${s.order.map(n=>`<option value="${n}" ${s.default===n?'selected':''}>${esc(memberName(n))}</option>`).join('')}</select></label><label><span>锁定</span><select id="instance-lock"><option value="">不锁定</option>${s.order.filter(n=>!s.disabled.includes(n)).map(n=>`<option value="${n}" ${s.locked===n?'selected':''}>${esc(memberName(n))}</option>`).join('')}</select></label><button type="button" data-action="instance-direct" ${l.selected[s.id]===0 || s.locked===0 ? 'disabled' : ''}>${s.direct?'移除 direct':'添加 direct'}</button><div class="form-error"></div><div class="form-actions"><button type="button" data-action="modal-close">取消</button><button class="primary">保存</button></div></form>`);
}
function rememberInstance() {
  const v=$('#instance-lock').value; instance().locked=v===''?null:Number(v);
  const d=$('#instance-default').value; instance().default=d===''?null:Number(d);
}

async function configSummary(request) {
  const d=await api('GET','configs'); if(request!==viewRequest)return;
  render(poolHead('配置','<a class="button" href="#/routes">管理链路 →</a>')+ (d.chains.length?`<div class="panel table-scroll"><table><thead><tr><th>名称</th><th>入口地址</th><th>状态</th><th>连接信息</th></tr></thead><tbody>${d.chains.map(c=>`<tr data-key="summary-${c.id}"><td><a href="#/routes/${c.link_id||''}">${esc(c.remark)}</a></td><td><code>${c.connection?esc(c.connection.host)+':'+c.connection.port:'等待入口确认'}</code></td><td>${c.link_state?linkState({state:c.link_state}):esc(c.status)}${c.link_error?`<small class="t-red">${esc(c.link_error)}</small>`:''}</td><td>${c.connection?`<button class="small" data-action="copy" data-text="${esc(c.connection.uri)}">复制${c.connection.protocol==='endpoint'?'地址':'导入链接'}</button>`:''}</td></tr>`).join('')}</tbody></table></div>`:emptyState('⇄','尚无配置，请先创建链路。')));
}

Object.assign(FORMS, {
  async 'quick-switch'() { const choice={...quickSwitch}; await api('POST',`links/${choice.id}/action`,{action:'switch',revision:choice.revision,stage:choice.stage,member:choice.member});closeModal();toast('等待节点确认');await refresh(); },
  async 'pool-save'(form,data) { await api(templateEditor.id?'PATCH':'POST',templateEditor.id?`pools/${templateEditor.id}`:'pools',{name:data.name,members:templateEditor.members});closeModal();refresh(); },
  async 'link-save'(form) { rememberLink(); const body={...linkEditor,apply:form.dataset.draft!=='1'};delete form.dataset.draft;const r=await api(body.id?'PATCH':'POST',body.id?`links/${body.id}`:'links',body);closeModal();location.hash=`#/routes/${r.id}`;refresh(); },
  async 'instance-save'() { rememberInstance();const l=instanceEditor.link;await api('PATCH',`links/${l.id}`,{name:l.name,revision:l.revision,plan:l.plan,apply:true});closeModal();refresh(); }
});
document.addEventListener('submit',e=>{if(e.target.dataset.form==='link-save')e.target.dataset.draft=e.submitter?.name==='draft'?'1':'0';},true);
Object.assign(ACTIONS, {
  'canvas-in':()=>topologyBinding?.zoom(1.2),
  'canvas-out':()=>topologyBinding?.zoom(1/1.2),
  'canvas-fit':()=>topologyBinding?.fit(),
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
  'topology-switch':el=>quickSwitchForm(poolView.link,el.dataset.stage,Number(el.dataset.member)),
  'instance-up'(el){rememberInstance();const i=Number(el.dataset.index),a=instance().order;[a[i-1],a[i]]=[a[i],a[i-1]];drawInstance();},
  'instance-down'(el){rememberInstance();const i=Number(el.dataset.index),a=instance().order;[a[i],a[i+1]]=[a[i+1],a[i]];drawInstance();},
  'instance-disable'(el){rememberInstance();const n=Number(el.dataset.member),s=instance();s.disabled=s.disabled.includes(n)?s.disabled.filter(x=>x!==n):[...s.disabled,n];if(s.locked===n)s.locked=null;drawInstance();},
  'instance-direct'(){rememberInstance();const s=instance();s.direct=!s.direct;s.order=s.direct?[...s.order,0]:s.order.filter(n=>n!==0);s.disabled=s.disabled.filter(n=>n!==0);if(!s.direct && s.default===0)s.default=s.order[0]??null;drawInstance();},
  'instance-switch':el=>quickSwitchForm(instanceEditor.source,instanceEditor.stage,Number(el.dataset.member))
});
