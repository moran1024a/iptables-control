// Templates provide membership; every link owns its selection and preferences.
const LINK_STATES = {draft:'草稿', active:'已应用', pending:'等待应用', preparing:'准备下游', switching:'切换入口', waiting:'等待确认', error:'异常', deleting:'删除中', manual:'等待手动同步', timeout:'确认超时', failed:'应用失败', offline:'节点离线'};
let poolView = null, linkEditor = null, templateEditor = null, instanceEditor = null, quickSwitch = null;
const expandedPools = new Set();
const resourceName = (items, id, fallback='已删除') => items.find(x => x.id === id)?.name || fallback;
const memberName = (id) => id === 0 ? 'direct' : resourceName(poolView.nodes, id);
const nodeNote = n => `${n.mode==='passive'?'被动':'主动'} · ${STATUS[n.status]?.[0]||'未就绪'}${n.mode!=='passive'&&!n.eligible?' · 待确认主动 Agent':''}`;
const nodeChoice = n => ({...n, note:nodeNote(n), disabled:n.status==='deleting'});
const poolMemberReason = n => n.status==='deleting'?'节点正在删除':n.mode==='passive'?'被动节点不能加入池':!n.eligible?'需要先确认安装主动 Agent':'';
const linkStatus = l => l.sync?.state || l.state;
const linkState = l => {
  const state=linkStatus(l), problem=['error','timeout','failed','offline'].includes(state);
  return `<span class="badge ${problem?'removing':state==='active'?'applied':'applying'}">${l.recovery_required?'需确认运行方案':l.pending_changes&&!l.job?'变更未生效':LINK_STATES[state] || esc(state)}</span>`;
};
const poolHead = (title, button='') => `<div class="page-head"><h1>${title}</h1><div class="head-actions">${button}</div></div>`;

const collectionFilters = {pools:{q:'',state:''}, links:{q:'',state:'',group:''}, configs:{q:'',state:'',group:''}};
filters.links=collectionFilters.links; filters.configs=collectionFilters.configs;
let drawCollection = null;
const matchesSearch = (kind, text) => text.toLowerCase().includes(collectionFilters[kind].q.trim().toLowerCase());
const statusGroup = l => {
  const state=linkStatus(l);
  return ['error','timeout','failed','offline'].includes(state)?'error':['preparing','switching','pending','waiting','manual'].includes(state)?'pending':state;
};
function collectionTools(kind, count, total, statuses, groups=[]) {
  const f=collectionFilters[kind];
  return `<div class="collection-tools" data-collection="${kind}"><input type="search" data-collection-query value="${esc(f.q)}" placeholder="搜索名称或地址" aria-label="搜索列表"><select data-collection-state aria-label="筛选状态"><option value="">全部状态</option>${statuses.map(([key,label])=>`<option value="${key}" ${f.state===key?'selected':''}>${label}</option>`).join('')}</select>${groups.length>1?`<select data-collection-group aria-label="筛选分组"><option value="">全部分组</option>${groups.map(g=>`<option value="${g.id}" ${String(g.id)===f.group?'selected':''}>${esc(g.name)}</option>`).join('')}</select>`:''}<span class="count">${count} / ${total}</span></div>`;
}
document.addEventListener('input',e=>{
  const box=e.target.closest('[data-collection]');if(!box || !e.target.hasAttribute('data-collection-query'))return;
  collectionFilters[box.dataset.collection].q=e.target.value;drawCollection?.();
});
document.addEventListener('change',e=>{
  const box=e.target.closest('[data-collection]');if(!box)return;
  const field=e.target.hasAttribute('data-collection-state')?'state':e.target.hasAttribute('data-collection-group')?'group':null;
  if(!field)return;collectionFilters[box.dataset.collection][field]=e.target.value;drawCollection?.();
});

async function poolList(request) {
  const d = await api('GET','pools'); if (request !== viewRequest) return; poolView = d;
  drawCollection=()=>{
    const rows=d.pools.filter(p=>matchesSearch('pools',p.name+' '+p.members.map(n=>memberName(n)+' '+(d.nodes.find(x=>x.id===n)?.addr||'')).join(' ')) && (!collectionFilters.pools.state || (p.references.length?'used':'free')===collectionFilters.pools.state));
    render(poolHead('池','<button class="primary" data-action="pool-new">新增池</button>')+
      collectionTools('pools',rows.length,d.pools.length,[['used','已引用'],['free','未引用']])+
      `<div class="collection">${rows.map(p=>`<div class="collection-row" data-key="pool-${p.id}"><div class="collection-main inline-summary"><button class="plain-title" data-action="pool-edit" data-id="${p.id}" title="${esc(p.name)}">${esc(p.name)}</button><div class="member-tags">${(expandedPools.has(p.id)?p.members:p.members.slice(0,3)).map(n=>`<span title="${esc(memberName(n))}">${esc(memberName(n))}</span>`).join('')||'<span>暂无成员</span>'}${p.members.length>3?`<button class="small" data-action="pool-expand" data-id="${p.id}" aria-expanded="${expandedPools.has(p.id)}">${expandedPools.has(p.id)?'收起':`+${p.members.length-3}`}</button>`:''}</div></div><span class="muted collection-meta" title="${esc(p.references.join('、'))}">${p.members.length} 个成员 · ${p.references.length?`${p.references.length} 条链路`:'未引用'}</span><div class="row-actions"><button class="small" data-action="pool-edit" data-id="${p.id}">编辑</button>${moreActions(`pool-actions-${p.id}`,`<button class="danger" data-action="pool-delete" data-id="${p.id}" ${p.references.length?'disabled title="请先解除链路引用"':''}>删除</button>`)}</div></div>`).join('')||emptyState('◉',d.pools.length?'没有匹配的池':'暂无池')}</div>`);
  };drawCollection();
}

const dragHandle = '<span class="drag-handle" title="拖动排序" aria-hidden="true">⠿</span>';
const orderButtons = (prefix,i,length) => `<button type="button" class="small" data-action="${prefix}-up" data-index="${i}" ${i?'':'disabled'} aria-label="上移">↑</button><button type="button" class="small" data-action="${prefix}-down" data-index="${i}" ${i<length-1?'':'disabled'} aria-label="下移">↓</button>`;
function poolForm(p) {
  templateEditor = {id:p?.id, name:p?.name || '', members:[...(p?.members || [])]}; drawPoolForm();
}
function drawPoolForm(preserve=false) {
  const p = templateEditor, available=poolView.nodes.filter(n=>!p.members.includes(n.id));
  openModal(`<form data-form="pool-save"><h2>${p.id ? '编辑池' : '新增池'}</h2><label><span>名称</span><input name="name" value="${esc(p.name)}" required maxlength="100"></label><div class="member-list" data-reorder="pool">${p.members.map((n,i)=>`<div class="member-row sort-item" data-order="${n}">${dragHandle}<span class="member-label">${i+1}. ${esc(memberName(n))}</span>${orderButtons('pool',i,p.members.length)}<button type="button" class="small danger" data-action="pool-remove" data-index="${i}">移除</button></div>`).join('')}</div><p class="hint">仅已完成迁移并确认安装的主动节点可加入池；旧节点请先在节点详情中复制一键接入命令完成升级。</p><details class="member-picker"><summary>添加成员 <span class="count">${available.filter(n=>!poolMemberReason(n)).length}</span></summary><div class="resource-menu"><input type="search" data-resource-search data-ignore-dirty placeholder="搜索名称或地址" aria-label="搜索成员"><div class="member-options">${available.map(n=>`<label data-search="${esc((n.name+' '+n.addr).toLowerCase())}"><input type="checkbox" value="${n.id}" data-member-choice data-ignore-dirty ${poolMemberReason(n)?'disabled':''}><span>${esc(n.name)}<small>${esc([n.addr,poolMemberReason(n)||nodeNote(n)].filter(Boolean).join(' · '))}</small></span></label>`).join('')}<span class="resource-empty" ${available.length?'hidden':''}>没有匹配项</span></div><button type="button" class="small" data-action="pool-add">添加所选成员</button></div></details><div class="form-error" role="alert"></div><div class="form-actions"><button type="button" data-action="modal-close">取消</button><button class="primary">保存</button></div></form>`,preserve);
}
function rememberPool() { templateEditor.name = $('#modal [name=name]').value; }

async function linkList(request) {
  const d = await api('GET','links'); if (request !== viewRequest) return; poolView = d;
  list={kind:'links',data:d,draw:()=>drawCollection()};
  drawCollection=()=>{
    const route=l=>{const p=l.applied_plan||l.plan;return [p.entry?resourceName(d.nodes,p.entry):null,...p.stages.map(st=>st.kind==='pool'?resourceName(d.pools,st.pool_id):memberName(st.node_id)),resourceName(d.targets,p.target,'未选择目标')].filter(Boolean).join(' → ');};
    if(sortMode==='links')return render(listHead('links',d.links.length,d.links.length)+sortList('links',d.links,l=>`<div class="title">${esc(l.name)}</div><div class="sub route-summary" title="${esc(route(l))}">${esc(route(l))}</div>`));
    const f=collectionFilters.links;
    const rows=d.links.filter(l=>matchesSearch('links',l.name+' '+route(l)+' '+(l.connection?l.connection.host+':'+l.connection.port:''))&&(!f.state || statusGroup(l)===f.state)&&(!f.group || l.group_id===Number(f.group)));
    const card=l=>`<a href="#/routes/${l.id}" class="collection-row link-card" data-key="link-${l.id}"><div class="collection-main"><div class="title">${esc(l.name)}</div><div class="sub route-summary" title="${esc(route(l))}">${esc(route(l))}</div>${l.error?`<div class="row-error">${esc(l.error)}</div>`:''}</div>${l.cleanup.length?`<span class="muted collection-meta">${l.cleanup.length} 项待清理</span>`:''}${linkState(l)}<span class="muted">›</span></a>`;
    render(listHead('links',d.links.length,rows.length,'link-new','新增链路')+
      collectionTools('links',rows.length,d.links.length,[['active','已应用'],['pending','等待同步'],['error','异常'],['draft','草稿'],['deleting','删除中']],d.groups)+
      `<div class="collection">${groupedList('links',rows,card,()=> '链路')||emptyState('⇄',d.links.length?'没有匹配的链路':'暂无链路')}</div>`);
  };drawCollection();
}

function switchBlocked(l, stage, member) {
  if (l.job) return '已有切换正在进行，请等待确认';
  if (l.state === 'deleting') return '链路正在删除';
  if (!l.chain_id) return '请先保存并应用链路';
  if (l.recovery_required || (l.pending_changes && !l.plan.stages.some(s=>s.id===stage.id && s.kind==='pool' && s.pool_id===stage.pool_id))) return '请先编辑并确认未生效的链路方案';
  if (stage.disabled.includes(member)) return '此成员已禁用';
  if (stage.locked != null && stage.locked !== member) return '请先在池设置中解除锁定';
  const node = poolView.nodes.find(n => n.id === member);
  if (member !== 0 && (!node?.eligible || node.status !== 'synced')) return node?.status === 'offline' ? '节点离线，暂不能切换' : '节点尚未就绪，请检查节点状态';
  if (l.selected[stage.id] === member && stage.default === member) return '当前默认节点已生效';
  return '';
}

function topology(l) {
  const d = poolView, plan = l.applied_plan || l.plan;
  // Fixed dimensions keep HTML cards and SVG cables aligned without layout observers.
  const cardH = 60, gap = 8, inset = 10, headerH = 46, gutter = 48;
  const columns = [];
  const fixed = (kind, id, label) => ({kind, id, label, width:180, height:84});
  if (plan.entry) columns.push(fixed('entry', plan.entry, '入口'));
  for (const stage of plan.stages) {
    columns.push(stage.kind === 'pool'
      ? {kind:'pool', stage, width:210, height:headerH + inset*2 + Math.max(1,stage.order.length)*(cardH+gap)-gap+2}
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
  const flowing=matches && healthy && !l.recovery_required && !['draft','deleting'].includes(l.state) && (!l.job || l.job.phase==='prepare');
  const shape=p=>JSON.stringify([p.entry,p.target,p.stages.map(s=>[s.id,s.kind,s.node_id,s.pool_id])]);
  const pending=l.job && shape(plan)===shape(l.job.plan||l.plan) ? columns.map(col=>picked(col,l.job.phase==='rollback' ? l.selected : l.job.selected)) : [];
  const cables=[], active=[], waiting=[], hits=[];
  const pathBetween=(a,b)=>{
    const x=a.x+a.width,bend=Math.min(32,(b.x-x)/4);
    return `M ${x} ${a.y} C ${x+bend} ${a.y} ${b.x-bend} ${b.y} ${b.x} ${b.y}`;
  };
  for(let i=0;i<columns.length-1;i++) {
    const left=columns[i],right=columns[i+1];
    for(const a of left.points) for(const b of right.points) {
      if(a.member && a.member===b.member && right.kind!=='target') continue;
      cables.push(`<path class="topology-candidate" data-from="${a.key}" data-to="${b.key}" d="${pathBetween(a,b)}"/>`);
      if(right.kind!=='target' && a.member && b.member && [a.member,b.member].every(id=>d.nodes.find(n=>n.id===id)?.mode==='active')) {
        hits.push(`<path class="topology-hit" data-action="edge-quality" data-source="${a.member}" data-target="${b.member}" d="${pathBetween(a,b)}" role="button" tabindex="0" aria-label="${esc(memberName(a.member)+' → '+memberName(b.member)+' 网络质量')}"><title>${esc(memberName(a.member)+' → '+memberName(b.member))} · 网络质量</title></path>`);
      }
    }
    if(matches) {
      const path=pathBetween(current[i],current[i+1]);
      active.push(`<path class="topology-confirmed${flowing?' flowing':''}" data-from="${current[i].key}" data-to="${current[i+1].key}" d="${path}"/>`);
      if(flowing) active.push(`<path class="topology-flow" data-from="${current[i].key}" data-to="${current[i+1].key}" d="${path}"/>`);
    }
    if(pending[i] && pending[i+1]) waiting.push(`<path class="topology-pending" data-from="${pending[i].key}" data-to="${pending[i+1].key}" d="${pathBetween(pending[i],pending[i+1])}"/>`);
  }
  const cards=columns.map((col,index)=>{
    if(col.kind!=='pool') {
      const node=d.nodes.find(n=>n.id===col.id),target=d.targets.find(t=>t.id===col.id);
      const name=col.kind==='target'?(target?.name||'未选择目标'):(node?.name||'已删除节点');
      const subtitle=col.kind==='entry'?`${node?.addr||''}:${plan.port||'自动端口'}`:col.kind==='target'?'':node?.addr||'';
      return `<div class="topology-fixed${flowing?' on-route':''}" data-key="fixed-${index}-${col.id}" data-point="${col.points[0].key}" ${matches?'data-current':''} style="left:${col.x}px;top:${col.y}px;width:${col.width}px"><small>${col.label}${col.kind!=='target'?` · ${node?.mode==='passive'?'被动':'主动'}`:''}</small>${col.kind==='target'?`<strong title="${esc(name)}">${esc(name)}</strong>`:`<a href="#/nodes/${col.id}" title="${esc(name)}">${esc(name)}</a>`}<span class="topology-address" title="${esc(subtitle)}">${esc(subtitle)}</span></div>`;
    }
    const st=col.stage;
    return `<section class="topology-pool" data-key="stage-${esc(st.id)}" aria-label="${esc(resourceName(d.pools,st.pool_id))}" style="left:${col.x}px;top:${col.y}px;width:${col.width}px;height:${col.height}px"><div class="topology-pool-head"><div><small>池 · ${st.order.length} 个候选${st.locked!=null?' · 已锁定':''}</small><strong title="${esc(resourceName(d.pools,st.pool_id))}">${esc(resourceName(d.pools,st.pool_id))}</strong></div><button class="small" data-action="instance-edit" data-stage="${esc(st.id)}" ${l.job||l.state==='deleting'||!l.plan.stages.some(s=>s.id===st.id&&s.kind==='pool'&&s.pool_id===st.pool_id)?'disabled':''}>设置</button></div><div class="topology-members">${col.points.map((p,i)=>{
      const node=d.nodes.find(n=>n.id===p.member),isCurrent=matches&&l.selected[st.id]===p.member,isPending=l.job?.selected[st.id]===p.member;
      const reason=switchBlocked(l,st,p.member),disabled=st.disabled.includes(p.member);
      const status=p.member===0?'':({synced:'在线',offline:'离线',syncing:'等待同步',new:'未连接',error:'异常',skew:'时间异常',deleting:'删除中'}[node?.status]||'未就绪');
      const label=isPending?'待确认':isCurrent?(flowing?'生效':'上次确认'):disabled?'禁用':'';
      return `<button class="topology-choice${isCurrent?' is-current':''}${isCurrent&&flowing?' on-route':''}${isPending?' is-pending':''}${disabled?' is-disabled':''}" data-key="member-${p.member}" ${isCurrent?'data-current':''} data-point="${p.key}" data-action="topology-switch" data-stage="${esc(st.id)}" data-member="${p.member}" title="${esc(reason||'切换到 '+memberName(p.member))}" ${reason?'disabled':''}><span class="topology-choice-title"><span class="topology-rank">${i+1}</span><strong title="${esc(memberName(p.member))}">${esc(p.member===0?'direct':node?.name||'已删除节点')}</strong>${st.default===p.member?'<small class="topology-default">默认</small>':''}</span><span class="topology-choice-meta"><span class="${p.member===0?'':'topology-node-status'} ${node?.status==='synced'||p.member===0?'ready':''}">${esc(status)}</span><span class="topology-state">${esc(label)}${st.locked===p.member?' · 锁定':''}</span></span></button>`;
    }).join('')||'<p class="hint">暂无成员，请打开设置</p>'}</div></section>`;
  }).join('');
  return `<div class="topology-toolbar"><div class="topology-legend"><span class="legend-active">生效</span><span class="legend-pending">待确认</span></div><div class="topology-tools"><button class="small" data-action="canvas-out" title="缩小" aria-label="缩小">−</button><span data-zoom-value>100%</span><button class="small" data-action="canvas-in" title="放大" aria-label="放大">+</button><button class="small" data-action="canvas-fit">适应</button><button class="small" data-action="canvas-current" ${matches?'':'disabled'}>当前路径</button><button class="small" data-action="canvas-fullscreen">${topologyViews.get(l.id)?.fullscreen?'退出全屏':'全屏'}</button></div></div><div class="topology" data-key="canvas-${l.id}" tabindex="0" role="region" aria-label="链路画布，拖动平移，滚轮缩放"><div class="topology-canvas" style="width:${width}px;height:${height}px">${cards}<svg class="topology-cables" width="${width}" height="${height}" viewBox="0 0 ${width} ${height}" aria-label="节点连线">${cables.join('')}${active.join('')}${waiting.join('')}${hits.join('')}</svg></div></div>`;
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
  const member=event.target.closest?.('[data-point]');
  if(!member || member.contains(event.relatedTarget)) return;
  const enabled=event.type==='mouseover'||event.type==='focusin', canvas=member.closest('.topology-canvas');
  canvas.classList.toggle('has-highlight',enabled);
  canvas.querySelectorAll('path[data-from]').forEach(path=>{
    path.classList.toggle('is-focused',enabled && (path.dataset.from===member.dataset.point || path.dataset.to===member.dataset.point));
  });
}
for(const event of ['mouseover','mouseout','focusin','focusout']) document.addEventListener(event,focusTopology);

const topologyViews = new Map();
let topologyBinding = null;

function releaseTopology(preserve = false) {
  topologyBinding?.observer.disconnect();
  if (!preserve) {
    if (topologyBinding) topologyBinding.view.fullscreen = false;
    document.body.classList.remove('canvas-fullscreen');
  }
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
  const focusCurrent = () => {
    const cards=[...canvas.querySelectorAll('[data-current]')]; if(!cards.length)return;
    const origin=canvas.getBoundingClientRect(), rects=cards.map(el=>el.getBoundingClientRect());
    const x=(Math.min(...rects.map(r=>r.left))-origin.left)/view.scale;
    const y=(Math.min(...rects.map(r=>r.top))-origin.top)/view.scale;
    const width=(Math.max(...rects.map(r=>r.right))-origin.left)/view.scale-x;
    const height=(Math.max(...rects.map(r=>r.bottom))-origin.top)/view.scale-y;
    view.scale=Math.min(1.5,(viewport.clientWidth-48)/width,(viewport.clientHeight-48)/height);
    view.x=(viewport.clientWidth-width*view.scale)/2-x*view.scale;
    view.y=(viewport.clientHeight-height*view.scale)/2-y*view.scale;
    view.auto=false;apply();
  };
  const fullscreen = () => {
    view.fullscreen=!view.fullscreen;
    viewport.closest('.topology-panel').classList.toggle('is-fullscreen',view.fullscreen);
    document.body.classList.toggle('canvas-fullscreen',view.fullscreen);
    app.querySelector('[data-action=canvas-fullscreen]').textContent=view.fullscreen?'退出全屏':'全屏';
    fit();
  };
  if (topologyBinding?.viewport !== viewport) {
    releaseTopology(topologyBinding?.view === view);
    const points = new Map();
    const position = e => { const r=viewport.getBoundingClientRect();return {x:e.clientX-r.left-viewport.clientLeft,y:e.clientY-r.top-viewport.clientTop}; };
    const midpoint = values => ({x:values.reduce((a,p)=>a+p.x,0)/values.length,y:values.reduce((a,p)=>a+p.y,0)/values.length});
    const distance = values => Math.hypot(values[0].x-values[1].x,values[0].y-values[1].y);
    viewport.addEventListener('wheel',e=>{e.preventDefault();const p=position(e);zoom(Math.exp(-Math.max(-300,Math.min(300,e.deltaY))*.002),p.x,p.y);},{passive:false});
    viewport.onpointerdown = e => {
      if (e.button!==0 || e.target.closest('button,a,input,select,[data-action]') || points.size>=2) return;
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
    topologyBinding={viewport,observer,fit,zoom,focusCurrent,fullscreen,view,apply};
  }
  viewport.closest('.topology-panel').classList.toggle('is-fullscreen',!!view.fullscreen);
  document.body.classList.toggle('canvas-fullscreen',!!view.fullscreen);
  if(view.auto) fit(); else apply();
}

function syncFeedback(l) {
  const pending=l.sync?.nodes||[];
  const notice=l.recovery_required?'旧运行方案不完整，请编辑并确认链路。':l.pending_changes&&!l.job?'变更未生效，当前仍使用已确认的路径。':'';
  if(notice)return `<div class="sync-feedback" role="status"><span>${notice}</span>${l.error?`<p class="row-error">${esc(l.error)}</p>`:''}<button class="small" data-action="link-retry">重试同步</button></div>`;
  if(!pending.length && !l.error && !l.job)return '';
  return `<div class="sync-feedback" role="status">${l.error?`<p class="row-error">${esc(l.error)}</p>`:''}${pending.length?`<div class="sync-nodes">${pending.map(n=>`<a href="#/nodes/${n.id}" title="${esc(n.error)}">${esc(n.name)} <span>${LINK_STATES[n.reason]||'等待确认'}</span></a>`).join('')}</div>`:l.job?'<span class="muted">等待节点确认</span>':''}${pending.some(n=>n.reason==='manual'||n.mode==='passive'&&['failed','timeout'].includes(n.reason))?'<span class="manual-sync">在对应节点执行 <code>sudo ictl --sync</code><button class="small" data-action="copy" data-text="sudo ictl --sync">复制</button></span>':''}${l.job?.phase==='rollback'?'<span class="muted">正在恢复旧路径</span>':l.job?'<button class="small" data-action="link-cancel">取消切换</button>':''}${l.sync?.retry || !l.sync&&l.error?'<button class="small" data-action="link-retry">重试同步</button>':''}</div>`;
}

document.addEventListener('keydown',e=>{
  if(e.key==='Escape' && $('#modal').hidden && topologyBinding?.view.fullscreen)topologyBinding.fullscreen();
  if((e.key==='Enter'||e.key===' ') && e.target.matches?.('.topology-hit')) {e.preventDefault();e.target.dispatchEvent(new MouseEvent('click',{bubbles:true}));}
});

async function linkQualityPage(id, request) {
  const range=qualityViews.get('links/'+id)?.range||900;
  let d;
  try { d=await api('POST','quality',{scope:'links',id,range}); }
  catch(err) {
    if(request!==viewRequest)return;
    if(err.status!==404)throw err;
    leaveView();
    render('<h1>链路不存在</h1><p class="muted">该链路可能已被删除。</p><a href="#/routes">返回链路列表</a>');
    return;
  }
  if(request!==viewRequest)return;
  const cards=d.sections.map(section=>{
    return `<section class="panel quality-card" data-key="quality-${section.id}"><div class="quality-heading"><h2><span class="quality-kind">${esc(section.kind)}</span> ${esc(section.label)}</h2>${qualityLegend(section.series)}</div>${qualityChart(section.series,d.now,range)}${qualityTable(section.series,d.now)}</section>`;
  }).join('');
  renderQuality(`<a class="back" href="#/routes/${id}">← ${esc(d.name)}</a>`+poolHead('网络质量',`<select data-link-quality-range="${id}" aria-label="时间范围">${QUALITY_RANGES.map(([n,label])=>`<option value="${n}" ${n===range?'selected':''}>${label}</option>`).join('')}</select>`)+cards,d.sections.map(section=>({series:section.series,now:d.now,seconds:range})));
}
document.addEventListener('change',e=>{
  if(!e.target.hasAttribute('data-link-quality-range'))return;
  qualityViews.set('links/'+e.target.dataset.linkQualityRange,{range:Number(e.target.value)});refresh();
});

async function linkDetail(id, request) {
  let d;
  try { d = await api('GET',`links/${id}`); }
  catch (err) {
    if (request !== viewRequest) return;
    if (err.status !== 404) throw err;
    leaveView(); poolView=null;
    render('<h1>链路不存在</h1><p class="muted">该链路可能已被删除。</p><a href="#/routes">返回链路列表</a>');
    return;
  }
  if (request !== viewRequest) return; poolView=d; const l=d.link;
  render(`<a class="back" href="#/routes">← 链路</a>` + poolHead(esc(l.name), `${linkState(l)}<button data-action="link-edit" ${l.job || l.state === 'deleting' ? 'disabled' : ''}>编辑链路</button><button class="danger" data-action="link-delete" ${l.job || l.state === 'deleting' ? 'disabled' : ''}>删除</button>`) +
    `<div class="panel topology-panel${topologyViews.get(l.id)?.fullscreen?' is-fullscreen':''}">${topology(l)}${syncFeedback(l)}${l.cleanup.length ? `<p class="hint">旧规则待清理：${l.cleanup.map(r=>`${esc(memberName(r.node_id))}:${r.port}`).join('、')}</p>` : ''}</div>` +
    `<div class="panel link-connection"><h2>当前连接</h2>${l.connection ? `<div class="entry"><code>${esc(l.connection.host)}:${l.connection.port}</code><button class="small" data-action="copy" data-text="${esc(l.connection.uri)}">复制${l.connection.protocol==='endpoint' ? '地址' : '导入链接'}</button></div>` : '<p class="muted">尚未确认生效</p>'}<a href="#/quality/links/${l.id}">网络质量 →</a></div>` +
    detailsBlock(`link-events-${id}`, `切换记录 <span class="count">${l.events.length}</span>`, l.events.length ? `<div class="table-scroll"><table><tbody>${l.events.map(e=>`<tr><td>${esc(fmtTime(e.ts))}</td><td>${esc(e.message)}</td></tr>`).join('')}</tbody></table></div>` : '<p class="muted">暂无记录</p>'));
  mountTopology(id);
}

function linkForm(l) {
  linkEditor = {id:l?.id, revision:l?.revision, group_id:l?.group_id, name:l?.name || '', plan:structuredClone(l?.plan || {entry:null,port:null,target:null,stages:[]})};
  drawLinkForm();
}
function rememberLink() {
  const f=$('#modal form'); if(!f || f.dataset.form!=='link-save') return;
  linkEditor.group_id=Number(f.elements.group_id?.value)||linkEditor.group_id; linkEditor.name=f.elements.name.value; linkEditor.plan.entry=Number(f.elements.entry.value)||null;
  linkEditor.plan.target=Number(f.elements.target.value)||null; linkEditor.plan.port=Number(f.elements.port.value)||null;
}
function drawLinkForm(preserve=false) {
  const p=linkEditor.plan;
  const nodes=poolView.nodes.map(nodeChoice);
  const stages=[...poolView.pools.map(x=>({id:'p:'+x.id,name:'池 · '+x.name,note:`${x.members.length} 个成员`})),...nodes.map(x=>({...x,id:'n:'+x.id}))];
  openModal(`<form data-form="link-save"><h2>${linkEditor.id?'编辑链路':'新增链路'}</h2><label><span>名称</span><input name="name" value="${esc(linkEditor.name)}" required></label>${groupSelect(poolView.groups,linkEditor.group_id)}<div class="inline-fields"><label><span>入口节点</span>${resourceSelect('entry',nodes,p.entry,'无入口（直达目标）')}</label><label><span>入口端口</span><input name="port" type="number" min="1" max="65535" value="${p.port||''}" placeholder="自动分配"></label></div><h3>中间路径</h3><div class="member-list" data-reorder="stage">${p.stages.map((st,i)=>`<div class="member-row sort-item" data-order="${esc(st.id)}">${dragHandle}<span class="member-label">${i+1}. ${st.kind==='pool'?'池 · '+esc(resourceName(poolView.pools,st.pool_id)):esc(memberName(st.node_id))}</span>${orderButtons('stage',i,p.stages.length)}<button type="button" class="small danger" data-action="stage-remove" data-index="${i}">移除</button></div>`).join('')||'<p class="muted">入口直接连接目标</p>'}</div><div class="inline-fields">${resourceSelect('stage-add',stages,'','选择池或固定节点',true)}<button type="button" data-action="stage-add">添加阶段</button></div><label><span>目标</span>${resourceSelect('target',poolView.targets,p.target,'未选择')}</label><div class="form-error" role="alert"></div><div class="form-actions"><button type="button" data-action="modal-close">取消</button>${!linkEditor.id || poolView.link?.state==='draft'?'<button name="draft" value="1">保存草稿</button>':''}<button class="primary">保存并应用</button></div></form>`,preserve);
  $('#stage-add').setAttribute('data-ignore-dirty','');
  if(!preserve)modalState.initial=modalValues();
}

function instanceForm(sid) {
  const l=poolView.link; instanceEditor={link:structuredClone(l),source:structuredClone(l),stage:sid}; drawInstance();
}
function instance() { return instanceEditor.link.plan.stages.find(s=>s.id===instanceEditor.stage); }
function drawInstance(preserve=false) {
  const s=instance(),l=instanceEditor.link;
  const nodeOrder=new Map(poolView.nodes.map((n,i)=>[n.id,i]));
  const members=s.order.map(id=>{
    const n=poolView.nodes.find(n=>n.id===id), disabled=s.disabled.includes(id);
    return {id,name:memberName(id),note:disabled?'已禁用':id===0?'':n?nodeNote(n):'节点不存在',
      disabled:disabled || (id!==0 && (!n?.eligible || n.status!=='synced'))};
  }).sort((a,b)=>(nodeOrder.get(a.id)??nodeOrder.size)-(nodeOrder.get(b.id)??nodeOrder.size));
  openModal(`<form data-form="instance-save"><h2>${esc(resourceName(poolView.pools,s.pool_id))}</h2><div class="member-list" data-reorder="instance">${s.order.map((n,i)=>`<div class="member-row sort-item" data-order="${n}">${dragHandle}<span class="member-label">${i+1}. ${esc(memberName(n))}${l.selected[s.id]===n?' <b class="t-green">生效</b>':''}${s.default===n?' <small>默认</small>':''}</span>${orderButtons('instance',i,s.order.length)}<button type="button" class="small" data-action="instance-disable" data-member="${n}" ${l.selected[s.id]===n?'disabled':''}>${s.disabled.includes(n)?'启用':'禁用'}</button></div>`).join('')}</div><label><span>默认节点</span>${resourceSelect('instance-default',members,s.default,'请选择',true)}</label><label><span>锁定</span>${resourceSelect('instance-lock',members,s.locked,'不锁定',true)}</label><button type="button" data-action="instance-direct" ${l.selected[s.id]===0 || s.locked===0 ? 'disabled' : ''}>${s.direct?'移除 direct':'添加 direct'}</button><div class="form-error" role="alert"></div><div class="form-actions"><button type="button" data-action="modal-close">取消</button><button class="primary">保存</button></div></form>`,preserve);
}

function rememberInstance() {
  const v=$('#instance-lock').value; instance().locked=v===''?null:Number(v);
  const d=$('#instance-default').value; instance().default=d===''?null:Number(d);
}

async function configSummary(request) {
  const d=await api('GET','configs'); if(request!==viewRequest)return;
  list={kind:'configs',data:{...d,configs:d.chains},draw:()=>drawCollection()};
  drawCollection=()=>{
    const view=c=>({state:c.link_state||c.status,sync:c.link_sync}),f=collectionFilters.configs;
    const rows=d.chains.filter(c=>matchesSearch('configs',c.remark+' '+(c.connection?c.connection.host+':'+c.connection.port:''))&&(!f.state || statusGroup(view(c))===f.state)&&(!f.group || c.group_id===Number(f.group)));
    const card=c=>`<div class="collection-row" data-key="summary-${c.id}"><div class="collection-main inline-summary"><a class="title" href="#/routes/${c.link_id||''}" title="${esc(c.remark)}">${esc(c.remark)}</a><div class="sub"><code>${c.connection?esc(c.connection.host)+':'+c.connection.port:'等待入口确认'}</code></div>${c.link_error?`<div class="row-error">${esc(c.link_error)}</div>`:''}</div>${linkState(view(c))}<div class="row-actions">${c.connection?`<button class="small" data-action="copy" data-text="${esc(c.connection.uri)}">复制${c.connection.protocol==='endpoint'?'地址':'导入链接'}</button>`:''}</div></div>`;
    render(poolHead('配置','<a class="button" href="#/routes">管理链路 →</a>')+
      collectionTools('configs',rows.length,d.chains.length,[['active','已应用'],['pending','等待同步'],['error','异常'],['deleting','删除中']],d.groups)+
      `<div class="collection">${groupedList('configs',rows,card,()=> '配置',false)||emptyState('⇄',d.chains.length?'没有匹配的配置':'暂无配置')}</div>`);
  };drawCollection();
}

document.addEventListener('reordered',e=>{
  const kind=e.target.dataset.reorder;if(!kind)return;
  const order=[...e.target.querySelectorAll('[data-order]')].map(el=>el.dataset.order);
  const before=kind==='pool'?templateEditor.members:kind==='stage'?linkEditor.plan.stages.map(s=>s.id):instance().order;
  if(order.join(',')===before.join(','))return;
  if(kind==='pool'){rememberPool();templateEditor.members=order.map(Number);markModalDirty();drawPoolForm(true);}
  if(kind==='stage'){rememberLink();linkEditor.plan.stages=order.map(id=>linkEditor.plan.stages.find(s=>s.id===id));markModalDirty();drawLinkForm(true);}
  if(kind==='instance'){rememberInstance();instance().order=order.map(Number);markModalDirty();drawInstance(true);}
});

Object.assign(FORMS, {
  async 'quick-switch'() { const choice={...quickSwitch}; await api('POST',`links/${choice.id}/action`,{action:'switch',revision:choice.revision,stage:choice.stage,member:choice.member});closeModal();toast('等待节点确认');await refresh(); },
  async 'pool-save'(form,data) { await api(templateEditor.id?'PATCH':'POST',templateEditor.id?`pools/${templateEditor.id}`:'pools',{name:data.name,members:templateEditor.members});closeModal();refresh(); },
  async 'link-save'(form) { rememberLink(); const body={...linkEditor,apply:form.dataset.draft!=='1'};delete form.dataset.draft;const r=await api(body.id?'PATCH':'POST',body.id?`links/${body.id}`:'links',body);closeModal();location.hash=`#/routes/${r.id}`;refresh(); },
  async 'instance-save'() { rememberInstance();const l=instanceEditor.link;await api('PATCH',`links/${l.id}`,{name:l.name,revision:l.revision,plan:l.plan,apply:true});closeModal();refresh(); }
});
document.addEventListener('submit',e=>{if(e.target.dataset.form==='link-save')e.target.dataset.draft=e.submitter?.name==='draft'?'1':'0';},true);
Object.assign(ACTIONS, {
  'pool-expand'(el) {
    const id=Number(el.dataset.id);
    expandedPools.has(id)?expandedPools.delete(id):expandedPools.add(id);
    drawCollection();
  },
  async 'link-reload'(el) {
    const form=el.closest('form'), kind=form.dataset.form;
    const id=kind==='link-save'?linkEditor.id:kind==='instance-save'?instanceEditor.link.id:quickSwitch.id;
    const stage=kind==='instance-save'?instanceEditor.stage:quickSwitch?.stage, member=quickSwitch?.member;
    if(modalChanged() && !await confirmBox('载入最新配置？','当前未保存的修改将被丢弃。','重新载入'))return;
    const session=modalState,page=pageVersion;
    const d=await withBusyForm(form,()=>api('GET',`links/${id}`));
    if(!d || session!==modalState || page!==pageVersion)return;
    poolView=d;closeModal();
    if(d.link.job || d.link.state==='deleting')toast('链路正在变更，请等待完成后再编辑',true);
    else if(kind==='link-save')linkForm(d.link);
    else if(kind==='instance-save' && d.link.plan.stages.some(s=>s.id===stage && s.kind==='pool'))instanceForm(stage);
    else if(kind==='quick-switch')quickSwitchForm(d.link,stage,member);
    else toast('该池已从链路移除',true);
    await refresh();
  },
  'canvas-in':()=>topologyBinding?.zoom(1.2),
  'canvas-out':()=>topologyBinding?.zoom(1/1.2),
  'canvas-fit':()=>topologyBinding?.fit(),
  'canvas-current':()=>topologyBinding?.focusCurrent(),
  'canvas-fullscreen':()=>topologyBinding?.fullscreen(),
  'edge-quality':()=>{location.hash=`#/quality/links/${poolView.link.id}`;},
  'pool-new':()=>poolForm(), 'pool-edit':el=>poolForm(poolView.pools.find(p=>p.id===Number(el.dataset.id))),
  async 'pool-delete'(el){if(!(await confirmBox('删除池','删除 <b>'+esc(poolView.pools.find(p=>p.id===Number(el.dataset.id)).name)+'</b>？','删除',true)))return;await api('DELETE',`pools/${el.dataset.id}`);refresh();},
  'pool-down'(el){rememberPool();const i=Number(el.dataset.index),a=templateEditor.members;[a[i],a[i+1]]=[a[i+1],a[i]];markModalDirty();drawPoolForm(true);},
  'pool-up'(el){rememberPool();const i=Number(el.dataset.index),a=templateEditor.members;[a[i-1],a[i]]=[a[i],a[i-1]];markModalDirty();drawPoolForm(true);},
  'pool-remove'(el){rememberPool();templateEditor.members.splice(Number(el.dataset.index),1);markModalDirty();drawPoolForm(true);},
  'pool-add'(){rememberPool();const ids=[...document.querySelectorAll('[data-member-choice]:checked:not(:disabled)')].map(el=>Number(el.value));if(!ids.length)return;templateEditor.members.push(...ids);markModalDirty();drawPoolForm(true);},
  'link-new':()=>linkForm(), 'link-edit':()=>linkForm(poolView.link),
  async 'link-delete'(){if(!(await confirmBox('删除链路','删除 <b>'+esc(poolView.link.name)+'</b> 并清理其转发规则？','删除',true)))return;await api('DELETE',`links/${poolView.link.id}`);location.hash='#/routes';},
  async 'link-cancel'(){const l=poolView.link;if(!(await confirmBox('取消切换',l.job.phase==='prepare'?'撤销新路径准备，保留当前路径？':'恢复旧路径，确认入口恢复后清理新路径？','确认')))return;await api('POST',`links/${l.id}/action`,{action:'cancel',revision:l.revision});refresh();},
  async 'link-retry'(){const l=poolView.link;await api('POST',`links/${l.id}/action`,{action:'retry',revision:l.revision});refresh();},
  'stage-add'(){rememberLink();const [kind,id]=$('#stage-add').value.split(':');if(!id)return;linkEditor.plan.stages.push({id:crypto.randomUUID?.()||String(Date.now()),kind:kind==='p'?'pool':'node',...(kind==='p'?{pool_id:Number(id)}:{node_id:Number(id)})});markModalDirty();drawLinkForm(true);},
  'stage-down'(el){rememberLink();const i=Number(el.dataset.index),a=linkEditor.plan.stages;[a[i],a[i+1]]=[a[i+1],a[i]];markModalDirty();drawLinkForm(true);},
  'stage-up'(el){rememberLink();const i=Number(el.dataset.index),a=linkEditor.plan.stages;[a[i-1],a[i]]=[a[i],a[i-1]];markModalDirty();drawLinkForm(true);},
  'stage-remove'(el){rememberLink();linkEditor.plan.stages.splice(Number(el.dataset.index),1);markModalDirty();drawLinkForm(true);},
  'instance-edit':el=>instanceForm(el.dataset.stage),
  'topology-switch':el=>quickSwitchForm(poolView.link,el.dataset.stage,Number(el.dataset.member)),
  'instance-up'(el){rememberInstance();const i=Number(el.dataset.index),a=instance().order;[a[i-1],a[i]]=[a[i],a[i-1]];markModalDirty();drawInstance(true);},
  'instance-down'(el){rememberInstance();const i=Number(el.dataset.index),a=instance().order;[a[i],a[i+1]]=[a[i+1],a[i]];markModalDirty();drawInstance(true);},
  'instance-disable'(el){rememberInstance();const n=Number(el.dataset.member),s=instance();s.disabled=s.disabled.includes(n)?s.disabled.filter(x=>x!==n):[...s.disabled,n];if(s.locked===n)s.locked=null;markModalDirty();drawInstance(true);},
  'instance-direct'(){rememberInstance();const s=instance();s.direct=!s.direct;s.order=s.direct?[...s.order,0]:s.order.filter(n=>n!==0);s.disabled=s.disabled.filter(n=>n!==0);if(!s.direct && s.default===0)s.default=s.order[0]??null;markModalDirty();drawInstance(true);},
  'instance-switch':el=>quickSwitchForm(instanceEditor.source,instanceEditor.stage,Number(el.dataset.member))
});
