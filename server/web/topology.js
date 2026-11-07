// A canvas edits a draft; only a confirmed, complete path reaches the rule engine.
let graphSession = null;
let graphBusy = false;
let graphDragConsumed = false;
const graphID = () => crypto.randomUUID?.() || `v-${Date.now()}-${Math.random().toString(16).slice(2)}`;
const graphChanged = () => graphBusy || !!graphSession?.dirty || !!graphSession?.editor?.dirty;
const graphName = v => v.kind === 'direct' ? 'direct' : resourceName(v.kind === 'target' ? poolView.targets : poolView.nodes, v.target_id || v.node_id);
const graphEnds = g => ['entry','target'].map(k=>g.vertices.find(v=>v.kind===k)?.id);
const graphCurrent = () => graphSession?.graph || poolView.link.active_graph || poolView.link.graph;
const graphPairs = path => new Set((path||[]).slice(1).map((b,i)=>`${path[i]}/${b}`));

async function graphWork(work) {
  if (graphBusy || graphDrag) return;
  graphBusy = true;
  document.querySelectorAll('[data-graph-mutate]').forEach(el=>el.disabled=true);
  try { return await work(); }
  catch(e) {
    if(graphSession?.editor)graphReturnFromPreview(graphSession);
    if(graphSession) { graphSession.error=e.message; graphSession.errorField=e.field||'';graphSession.conflict=e.status===409;drawGraph(); }
    else toast(e.message,true);
  } finally {
    graphBusy=false;
    graphButtons();
  }
}

async function graphNavigate(id) {
  graphSession=null;graphBusy=false;
  const hash=`#/routes/${id}`;
  if(location.hash===hash)await refresh();else location.hash=hash;
}

function graphBounds(g, group) {
  if(['x','y','width','height'].every(k=>Number.isFinite(group[k])))
    return {x:group.x,y:group.y,width:group.width,height:group.height};
  const members=g.vertices.filter(v=>v.group===group.id),index=g.groups.indexOf(group);
  const x=members.length?Math.min(...members.map(v=>v.x))-20:group.x??40+index*280;
  const y=members.length?Math.min(...members.map(v=>v.y))-44:group.y??70;
  return {x,y,width:Math.max(260,...members.map(v=>v.x+200-x)),height:Math.max(180,...members.map(v=>v.y+84-y))};
}

function graphInitContainers(g) {
  g.groups.forEach(p=>Object.assign(p,graphBounds(g,p)));
}

function graphWorld(g) {
  const boxes=[...g.vertices.map(v=>({...v,width:180,height:60})),...g.groups.map(p=>graphBounds(g,p))];
  const x=boxes.length?Math.min(...boxes.map(b=>b.x-60)):0,y=boxes.length?Math.min(...boxes.map(b=>b.y-100)):0;
  return {x,y,width:Math.max(700,...boxes.map(b=>b.x+b.width+60-x)),height:Math.max(320,...boxes.map(b=>b.y+b.height+60-y))};
}

function graphAddPosition(width,height) {
  const canvas=app.querySelector('.topology-canvas'),view=topologyBinding.view,viewport=topologyBinding.viewport;
  let x=Math.round((viewport.clientWidth/2-view.x)/view.scale+Number(canvas.dataset.worldX)-width/2);
  let y=Math.round((viewport.clientHeight/2-view.y)/view.scale+Number(canvas.dataset.worldY)-height/2);
  const g=graphSession.graph,boxes=[...g.vertices.map(v=>({...v,width:180,height:60})),...g.groups.map(p=>graphBounds(g,p))];
  // New resources are independent objects, even when a pool is selected.
  for(let tries=0;tries<=boxes.length;tries++) {
    const collision=boxes.find(b=>x<b.x+b.width+20&&x+width>b.x-20&&y<b.y+b.height+20&&y+height>b.y-20);
    if(!collision)break;
    x=collision.x+collision.width+40;
  }
  return {x,y};
}

function graphConnections(g) {
  const owner=new Map(g.vertices.map(v=>[v.id,v.group||v.id]));
  return [...new Map(g.edges.map(([a,b])=>{const pair=[owner.get(a)||a,owner.get(b)||b];return [pair.join('/'),pair];})).values()];
}

function graphRenderEdges(g,editing) {
  const edges=graphConnections(g);
  if(editing)return edges;
  const members=id=>g.groups.some(p=>p.id===id)?g.vertices.filter(v=>v.group===id).map(v=>v.id):[id];
  return edges.flatMap(([a,b])=>members(a).flatMap(x=>members(b).map(y=>[x,y])));
}

function graphBoxes(g,editing) {
  const boxes=new Map(g.vertices.map(v=>[v.id,{...v,width:180,height:60}]));
  g.groups.forEach(p=>boxes.set(p.id,{id:p.id,...graphBounds(g,p)}));
  return boxes;
}

function graphPathBlocked(points,boxes,padding=14) {
  const minX=Math.min(...points.map(p=>p[0])),maxX=Math.max(...points.map(p=>p[0]));
  const minY=Math.min(...points.map(p=>p[1])),maxY=Math.max(...points.map(p=>p[1]));
  return boxes.some(b=>{
    const low=[b.x-padding,b.y-padding],high=[b.x+b.width+padding,b.y+b.height+padding];
    if(high[0]<minX||low[0]>maxX||high[1]<minY||low[1]>maxY)return false;
    return points.slice(1).some((q,i)=>{
      const p=points[i];let start=0,end=1;
      for(let axis=0;axis<2;axis++) {
        const delta=q[axis]-p[axis];
        if(!delta){if(p[axis]<low[axis]||p[axis]>high[axis])return false;continue;}
        const a=(low[axis]-p[axis])/delta,c=(high[axis]-p[axis])/delta;
        start=Math.max(start,Math.min(a,c));end=Math.min(end,Math.max(a,c));
        if(start>end)return false;
      }
      return true;
    });
  });
}

function graphRoundedPath(points) {
  const p=points.filter((v,i)=>!i||v[0]!==points[i-1][0]||v[1]!==points[i-1][1]);
  const xy=v=>v.map(n=>Math.round(n*100)/100).join(' ');
  let path=`M ${xy(p[0])}`;
  for(let i=1;i<p.length-1;i++) {
    const a=p[i-1],b=p[i],c=p[i+1],before=Math.hypot(b[0]-a[0],b[1]-a[1]),after=Math.hypot(c[0]-b[0],c[1]-b[1]);
    const r=Math.min(14,before/2,after/2);
    const entry=b.map((n,axis)=>n+(a[axis]-n)*r/before),exit=b.map((n,axis)=>n+(c[axis]-n)*r/after);
    path+=` L ${xy(entry)} Q ${xy(b)} ${xy(exit)}`;
  }
  return path+` L ${xy(p.at(-1))}`;
}

function graphCurve(a,b,boxes) {
  const source=boxes.get(a.group)||a,target=boxes.get(b.group)||b;
  const start=[a.x+a.width,a.y+a.height/2],end=[b.x,b.y+b.height/2];
  const excluded=new Set([a.id,b.id,source.id,target.id]);
  const obstacles=[...boxes.values()].filter(v=>!excluded.has(v.id)&&v.group!==a.id&&v.group!==b.id);
  const barriers=[...obstacles,source,target];
  const exit=source.x+source.width+24,enter=target.x-24;
  // Leave pools horizontally. Test the actual curve, not its diagonal chord.
  if(enter>=exit) {
    const bend=Math.min(90,(enter-exit)/2),points=[start];
    for(let step=0;step<=24;step++) {
      const t=step/24,u=1-t;
      points.push([u*u*u*exit+3*u*u*t*(exit+bend)+3*u*t*t*(enter-bend)+t*t*t*enter,
        (u*u*u+3*u*u*t)*start[1]+(3*u*t*t+t*t*t)*end[1]]);
    }
    points.push(end);
    if(!graphPathBlocked(points,obstacles)) {
      if(start[1]===end[1])return `M ${start.join(' ')} L ${end.join(' ')}`;
      return `M ${start.join(' ')} L ${exit} ${start[1]} C ${exit+bend} ${start[1]}, ${enter-bend} ${end[1]}, ${enter} ${end[1]} L ${end.join(' ')}`;
    }
  }
  // Cross-column and backward edges use a clear lane with rounded corners.
  // Every edge remains independent; there is no shared junction or hub.
  const column=v=>[...boxes.values()].filter(r=>r.x<=v.x+v.width/2&&r.x+r.width>=v.x+v.width/2);
  const exits=[exit,Math.max(exit,...column(source).map(v=>v.x+v.width+24))];
  const entries=[enter,Math.min(enter,...column(target).map(v=>v.x-24))];
  let best=null;
  for(const x of new Set(exits))for(const z of new Set(entries)) {
    if(graphPathBlocked([start,[x,start[1]]],obstacles)||graphPathBlocked([[z,end[1]],end],obstacles))continue;
    const relevant=barriers.filter(v=>v.x+v.width+14>=Math.min(x,z)&&v.x-14<=Math.max(x,z));
    const lanes=[...new Set(relevant.flatMap(v=>[v.y-32,v.y+v.height+32]))];
    lanes.sort((u,v)=>Math.abs(u-start[1])+Math.abs(u-end[1])-Math.abs(v-start[1])-Math.abs(v-end[1]));
    for(const y of lanes) {
      const core=[[x,start[1]],[x,y],[z,y],[z,end[1]]];
      if(graphPathBlocked(core,barriers))continue;
      const length=Math.abs(x-start[0])+Math.abs(z-x)+Math.abs(end[0]-z)+Math.abs(y-start[1])+Math.abs(y-end[1]);
      if(!best||length<best.length)best={length,points:[start,...core,end]};
      break;
    }
  }
  if(best)return graphRoundedPath(best.points);
  // Overlapping components in a free-form draft may leave no clear corridor.
  return graphRoundedPath([start,[exit,start[1]],[exit,Math.min(source.y,target.y)-32],[enter,Math.min(source.y,target.y)-32],[enter,end[1]],end]);
}

function graphPorts(id,kind='pool') {
  return `${kind==='entry'?'':`<button class="graph-port in" data-action="graph-port" data-side="in" data-id="${esc(id)}" title="连接到这里" aria-label="连线终点">●</button>`}${kind==='target'?'':`<button class="graph-port out" data-action="graph-port" data-side="out" data-id="${esc(id)}" title="从这里连线" aria-label="连线起点">●</button>`}`;
}

function graphFocusWires(event) {
  if(graphSession?.kind==='edit')return;
  const canvas=app.querySelector('.graph-viewport .topology-canvas');if(!canvas)return;
  const leaving=event.type==='mouseout'||event.type==='focusout';
  const target=leaving?event.relatedTarget:event.target;
  const item=target instanceof Element?target.closest('[data-graph-node],[data-graph-group]'):null;
  const id=item?.dataset.graphNode||item?.dataset.graphGroup||'';
  if(canvas.dataset.graphFocus===id)return;
  canvas.dataset.graphFocus=id;canvas.classList.toggle('graph-focus',!!id);
  const members=new Set([id,...graphCurrent().vertices.filter(v=>v.group===id).map(v=>v.id)]);
  canvas.querySelectorAll('[data-graph-from]').forEach(path=>path.classList.toggle('graph-nearby',members.has(path.dataset.graphFrom)||members.has(path.dataset.graphTo)));
}
for(const event of ['mouseover','mouseout','focusin','focusout'])document.addEventListener(event,graphFocusWires);

function graphCanvas(g, l) {
  const editing=graphSession?.kind==='edit', choosing=graphSession?.kind==='choose';
  const owner=id=>g.vertices.find(v=>v.id===id)?.group||id;
  const pairs=path=>graphPairs(editing?(path||[]).map(owner):path);
  const active=pairs(l?.active_path), pending=pairs(choosing?graphSession.prefix:l?.job?.plan?.graph_path);
  const on=new Set(l?.active_path||[]), options=new Set(choosing?graphSession.options:[]), chosen=new Set(choosing?graphSession.prefix:[]);
  const boxes=graphBoxes(g,editing), edges=graphRenderEdges(g,editing);
  const curveBoxes=boxes;
  const world=graphWorld(g),{width,height}=world;
  const groups=g.groups.map(p=>{
    const b=graphBounds(g,p),selected=graphSession?.selected===p.id;
    const empty=!g.vertices.some(v=>v.group===p.id);
    return `<section class="graph-pool ${selected?'graph-selected':''} ${graphSession?.errorField===`group:${p.id}`?'graph-invalid':''} ${editing?'is-editable':''}" data-key="graph-pool-${esc(p.id)}" data-graph-group="${esc(p.id)}" style="left:${b.x-world.x}px;top:${b.y-world.y}px;width:${b.width}px;height:${b.height}px">${editing?`<button class="graph-pool-body" data-action="graph-group" data-id="${esc(p.id)}" data-graph-drag="group" aria-label="选择和移动池：${esc(p.name)}"><span class="graph-pool-title">${esc(p.name)}${p.locked?' · 锁定':''}</span>${empty?'<span class="graph-pool-empty">拖入节点</span>':''}</button>${graphPorts(p.id)}${selected?`<button class="graph-resize" data-graph-drag="resize" data-id="${esc(p.id)}" title="调整池大小" aria-label="调整池大小"></button>`:''}`:`<span class="graph-pool-title">${esc(p.name)}${p.locked?' · 锁定':''}</span>`}</section>`;

  }).join('');
  const cards=g.vertices.map(v=>{
    const n=poolView.nodes.find(n=>n.id===v.node_id), current=on.has(v.id), candidate=options.has(v.id);
    const selected=graphSession?.selected===v.id, bad=graphSession?.errorField===`vertex:${v.id}`;
    const lock=g.groups.find(p=>p.id===v.group)?.locked,unavailable=v.disabled||lock&&lock!==v.id;
    return `<div class="graph-node ${current?'on-route':''} ${candidate?'graph-option':''} ${chosen.has(v.id)?'graph-chosen':''} ${selected?'graph-selected':''} ${unavailable?'graph-disabled':''} ${bad?'graph-invalid':''}" data-key="graph-node-${esc(v.id)}" data-graph-node="${esc(v.id)}" ${current?'data-current':''} style="left:${v.x-world.x}px;top:${v.y-world.y}px"><button class="graph-node-body" data-action="graph-select" data-id="${esc(v.id)}" ${editing?'data-graph-drag="node"':unavailable?'disabled':''}><small>${v.kind==='entry'?'入口':v.kind==='target'?'目标':v.group?'池成员':'节点'}${n?' · '+(n.mode==='passive'?'被动':'主动'):''}${v.disabled?' · 禁用':''}</small><strong title="${esc(graphName(v))}">${esc(graphName(v))}</strong><span>${candidate?'选择下一跳':chosen.has(v.id)?'已选择':current?'当前路径':n?STATUS[n.status]?.[0]||'未就绪':''}</span></button>${editing&&!v.group?graphPorts(v.id,v.kind):''}</div>`;
  }).join('');
  const order=({a,b,i})=>(active.has(`${a}/${b}`)?1:0)+(pending.has(`${a}/${b}`)?2:0)+(editing&&graphSession?.edge===i?4:0);
  const lines=edges.map(([a,b],i)=>({a,b,i})).sort((a,b)=>order(a)-order(b)).map(({a,b,i})=>{
    if(!boxes.has(a)||!boxes.has(b))return '';
    const key=`${a}/${b}`,d=graphCurve(boxes.get(a),boxes.get(b),curveBoxes);
    return `<path data-graph-line="${i}" data-graph-from="${esc(a)}" data-graph-to="${esc(b)}" class="graph-line ${active.has(key)?'graph-live':''} ${pending.has(key)?'graph-next':''} ${editing&&graphSession?.edge===i?'graph-selected-line':''}" d="${d}" marker-end="url(#graph-arrow)"/>${!editing&&active.has(key)&&l?.state==='active'?`<path data-graph-flow="${i}" data-graph-from="${esc(a)}" data-graph-to="${esc(b)}" class="topology-flow" d="${d}"/>`:''}${editing?`<path data-graph-hit="${i}" class="graph-hit" d="${d}" data-action="graph-edge" data-index="${i}"/>`:''}`;
  }).join('');
  return `<div class="topology-toolbar"><div class="topology-legend">${editing?`<button class="small" data-action="graph-add-open" data-graph-mutate>＋ 添加组件</button><div class="graph-history"><button class="small" data-action="graph-undo" data-graph-mutate title="撤销 (Ctrl/⌘ Z)" aria-label="撤销">↶</button><button class="small" data-action="graph-redo" data-graph-mutate title="重做 (Ctrl/⌘ Shift Z)" aria-label="重做">↷</button></div><span class="graph-editor-hint">${graphSession.connect?'点击终点 · Esc 取消':'拖动布局 · 圆点连线'}</span>`:'<span class="legend-active">生效</span><span class="legend-pending">待确认</span>'}</div><div class="topology-tools"><button class="small" data-action="canvas-out" aria-label="缩小">−</button><button class="small" data-zoom-value data-action="graph-actual-size" title="恢复 100% 大小">100%</button><button class="small" data-action="canvas-in" aria-label="放大">+</button><button class="small" data-action="canvas-fit">适应</button><button class="small" data-action="canvas-fullscreen">${topologyViews.get(graphSession?.id||l?.id||'new')?.fullscreen?'退出全屏':'全屏'}</button></div></div><div class="topology graph-viewport" tabindex="0" aria-label="拓扑画布"><div class="topology-canvas" data-world-x="${world.x}" data-world-y="${world.y}" style="width:${width}px;height:${height}px">${groups}<svg class="topology-cables" width="${width}" height="${height}" viewBox="${world.x} ${world.y} ${width} ${height}"><defs><marker id="graph-arrow" viewBox="0 0 10 10" refX="10" refY="5" markerWidth="5" markerHeight="5" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" fill="context-stroke"/></marker></defs>${lines}</svg>${cards}</div></div>`;
}

const graphPick=(items,value,empty)=>`<option value="">${empty}</option>`+items.map(x=>`<option value="${esc(x.id)}" ${String(value)===String(x.id)?'selected':''}>${esc(x.name)}</option>`).join('');

function graphEditorControls() {
  const s=graphSession,g=s.graph,v=g.vertices.find(v=>v.id===s.selected),p=g.groups.find(p=>p.id===s.selected);
  const choices=v?(v.kind==='target'?poolView.targets:poolView.nodes):[];
  const resource=v?.kind==='direct'?'<strong>direct</strong>':v?`<select data-graph-resource aria-label="${v.kind==='target'?'目标':'节点'}">${choices.map(n=>`<option value="${n.id}" ${(v.target_id||v.node_id)===n.id?'selected':''} ${v.group&&!n.eligible?'disabled':''}>${esc(n.name)}</option>`).join('')}</select>`:'';
  const inspector=v?`${resource}${v.group?`<span class="muted">${esc(g.groups.find(p=>p.id===v.group)?.name||'池成员')}</span>`:''}<label class="inline-check"><input type="checkbox" data-graph-property="disabled" ${v.disabled?'checked':''}>禁用</label><button class="small danger" data-action="graph-remove" data-graph-mutate>删除</button>`:p?`<input data-graph-group-name maxlength="64" value="${esc(p.name)}" aria-label="池名称"><select data-graph-lock aria-label="锁定成员">${graphPick(g.vertices.filter(v=>v.group===p.id&&!v.disabled).map(v=>({id:v.id,name:graphName(v)})),p.locked,'不锁定')}</select><button class="small danger" data-action="graph-remove-group" data-graph-mutate>解散池</button>`:s.edge!=null?`<span>已选连线</span><button class="small danger" data-action="graph-remove-edge" data-graph-mutate>删除连线</button>`:'';
  return `<div class="graph-meta"><input data-graph-field="name" value="${esc(s.name)}" maxlength="64" placeholder="链路名称" aria-label="链路名称"><select data-graph-field="group_id" aria-label="链路分组">${graphPick(poolView.groups,s.group_id,'默认分组')}</select><input data-graph-field="port" value="${s.port||''}" type="number" min="1" max="65535" placeholder="入口端口（自动）" aria-label="入口端口"></div>${inspector?`<div class="graph-inspector">${inspector}</div>`:''}`;
}

function graphAddDialog(kind) {
  const s=graphSession;
  kind=kind||(!s.graph.vertices.some(v=>v.kind==='entry')?'entry':'node');
  openModal(`<h2>添加组件</h2><label>类型<select id="graph-add-kind">${[['entry','入口'],['pool','池'],['node','节点'],['target','目标']].map(([id,name])=>`<option value="${id}" ${id===kind?'selected':''}>${name}</option>`).join('')}</select></label>${kind==='pool'?'<label>池名称<input id="graph-pool-name" placeholder="池名称" maxlength="64"></label>':`<label>${kind==='target'?'目标':'节点'}<select id="graph-add-resource">${graphPick(kind==='target'?poolView.targets:poolView.nodes,'','选择资源')}</select></label>`}<div class="form-actions"><button data-action="modal-close">取消</button><button class="primary" data-action="${kind==='pool'?'graph-add-group':'graph-add-node'}">添加</button></div>`);
}

function drawGraph() {
  const s=graphSession,l=poolView.link,g=graphCurrent();
  const title=s?.kind==='edit'?(s.id?'编辑拓扑':'新建拓扑'):esc(s?.name||l?.name||'选择路径');
  let actions,controls='';
  if(s?.kind==='edit') {
    actions='<button data-action="graph-exit" data-graph-mutate>取消</button><button data-action="graph-save-draft" data-graph-mutate>保存草稿</button><button class="primary" data-action="graph-publish" data-graph-mutate>校验并应用</button>';
    controls=graphEditorControls();
  } else if(s?.kind==='choose') {
    actions=`<button data-action="graph-exit" data-graph-mutate>${s.editor?'返回编辑':'取消选路'}</button>`;
    controls=`<div class="graph-selection">${s.prefix.map(i=>`<button class="plain-title" data-action="graph-rewind" data-id="${esc(i)}" data-graph-mutate>${esc(graphName(g.vertices.find(v=>v.id===i)))}</button>`).join(' → ')}${s.complete?'':' → 选择下一跳'}</div>`;
  } else actions=`${linkState(l)}<button data-action="link-edit" ${l.job||l.state==='deleting'?'disabled':''}>编辑</button><button class="danger" data-action="link-delete" ${l.job||l.state==='deleting'?'disabled':''}>删除</button>`;
  render(`<a class="back" href="#/routes">← 链路</a>${poolHead(title,actions)}<section class="panel topology-panel${topologyViews.get(s?.id||l?.id||'new')?.fullscreen?' is-fullscreen':''}"><div class="graph-fullscreen-actions">${actions}</div>${s?.error?`<div class="graph-error" role="alert">${esc(s.error)}${s.conflict&&s.id?'<button class="small" data-action="graph-reload" data-graph-mutate>重新载入</button>':s.kind==='choose'&&s.complete&&s.id?'<button class="small" data-action="graph-confirm" data-graph-mutate>重试</button>':''}</div>`:''}${s?.notice?`<div class="graph-notice" role="status">${esc(s.notice)}</div>`:''}${controls}${graphCanvas(g,l)}${!s?syncFeedback(l):''}</section>${!s?`${l.draft?'<div class="graph-notice">有已保存的编辑草稿 <button data-action="link-edit">继续编辑</button></div>':''}<div class="panel link-connection"><h2>当前连接</h2>${l.connection?`<code>${esc(l.connection.host)}:${l.connection.port}</code> <button class="small" data-action="copy" data-text="${esc(l.connection.uri)}">复制</button>`:'<span class="muted">尚未确认生效</span>'}<a href="#/quality/links/${l.id}">网络质量 →</a></div>${detailsBlock('graph-events-'+l.id,'切换记录',l.events.map(e=>`<p>${esc(fmtTime(e.ts))} ${esc(e.message)}</p>`).join('')||'暂无记录')}`:''}`);
  if(s?.kind==='edit') {
    app.querySelectorAll('[data-graph-field]').forEach(el=>{el.value=s[el.dataset.graphField]??'';el.setAttribute('aria-invalid',String(s.errorField===el.dataset.graphField));});
    const member=g.vertices.find(v=>v.id===s.selected),pool=g.groups.find(p=>p.id===s.selected);
    const resource=app.querySelector('[data-graph-resource]');if(resource)resource.value=member?.target_id||member?.node_id;
    const disabled=app.querySelector('[data-graph-property="disabled"]');if(disabled)disabled.checked=!!member?.disabled;
    const name=app.querySelector('[data-graph-group-name]');if(name)name.value=pool.name;
    const lock=app.querySelector('[data-graph-lock]');if(lock)lock.value=pool.locked??'';
  }
  const key=s?.id||l?.id||'new',view=topologyViews.get(key),world=graphWorld(g);
  if(view?.graphOrigin) {
    view.x+=(world.x-view.graphOrigin.x)*view.scale;
    view.y+=(world.y-view.graphOrigin.y)*view.scale;
  }
  mountTopology(key);
  topologyBinding.view.graphOrigin={x:world.x,y:world.y};
  graphButtons();
  graphPaintConnection();
}

async function graphDetail(id,request) {
  if(graphSession?.id===id)return;
  let d;
  try { d=await api('GET',`links/${id}`); }
  catch(e) { if(request!==viewRequest)return;if(e.status!==404)throw e;leaveView();poolView=null;render('<h1>链路不存在</h1><a href="#/routes">返回链路</a>');return; }
  if(request!==viewRequest)return;
  poolView=d;drawGraph();
}

function startGraphEditor(l) {
  const draft=l?.draft;
  graphSession={kind:'edit',id:l?.id,revision:l?.revision,name:draft?.name??l?.name??'',group_id:draft?.group_id||l?.group_id||poolView.groups[0]?.id,
    port:draft?draft.port:l?.plan.port??null,graph:structuredClone(draft?.graph||l?.graph||{version:1,vertices:[],groups:[],edges:[]}),dirty:false,addKind:'node'};
  graphSession.graph.edges=graphConnections(graphSession.graph);
  graphInitContainers(graphSession.graph);
  graphSession.initial=graphSession.editBase=graphSnapshot(graphSession);graphSession.undo=[];graphSession.redo=[];
  drawGraph();
}

async function graphNew(request) {
  if(graphSession?.kind==='edit'&&!graphSession.id)return;
  const d=await api('GET','links');if(request!==viewRequest)return;
  poolView=d;startGraphEditor(null);
}

function graphValidateFields(s) {
  const invalid=(message,field)=>{const error=new Error(message);error.field=field;throw error;};
  if(!s.name.trim()||s.name.trim().length>64)invalid('名称需要包含 1 至 64 个字符','name');
  const input=app.querySelector('[data-graph-field="port"]');
  if((input&&!input.checkValidity())||(s.port!=null&&(!Number.isInteger(s.port)||s.port<1||s.port>65535)))invalid('端口必须是 1–65535 的整数','port');
}

const graphSnapshot=s=>JSON.stringify({graph:s.graph,name:s.name,group_id:s.group_id,port:s.port});

function graphButtons() {
  const s=graphSession;
  document.querySelectorAll('[data-graph-mutate]').forEach(el=>{
    el.disabled=graphBusy||!!graphDrag||el.dataset.action==='graph-undo'&&!s?.undo?.length||el.dataset.action==='graph-redo'&&!s?.redo?.length;
  });
}

function graphTouch(record=true) {
  const s=graphSession;
  if(record&&s.kind==='edit') {
    const current=graphSnapshot(s);
    if(s.editBase!==current) {
      if(s.editBase)(s.undo??=[]).push(s.editBase);
      if(s.undo?.length>50)s.undo.shift();
      s.redo=[];s.editBase=current;
    }
  }
  s.dirty=graphSnapshot(s)!==s.initial;
  s.notice='';if(!s.conflict){s.error='';s.errorField='';}
  graphButtons();
}

function graphUndo(redo=false) {
  const s=graphSession;if(s?.kind!=='edit'||graphBusy||graphDrag)return;
  graphTouch();
  const source=redo?s.redo:s.undo,target=redo?s.undo:s.redo;
  if(!source.length)return;
  target.push(graphSnapshot(s));Object.assign(s,JSON.parse(source.pop()));
  s.editBase=graphSnapshot(s);s.selected=null;s.edge=null;s.connect=null;graphTouch(false);drawGraph();
}

async function graphPreview(s,prefix,ready=true,arrange=false,advance=false) {
  return api('POST','topology-preview',{graph:s.graph,prefix,required:s.required||[],preferred:s.preferred||[],ready,arrange,advance});
}

async function advanceGraph(s,auto=true,prefix=s.prefix) {
  const r=await graphPreview(s,prefix,true,false,auto);
  if(graphSession!==s)return;
  s.prefix=r.prefix;s.options=r.options;s.complete=r.complete;
  drawGraph();
  if(s.complete&&auto)await confirmGraphChoice(s);
}

async function confirmGraphChoice(s) {
  if(!s?.complete)return;
  const names=s.prefix.map(id=>esc(graphName(s.graph.vertices.find(v=>v.id===id)))).join(' → ');
  const accepted=await confirmBox(s.editor?'应用拓扑？':'切换路径？',names,s.editor?'应用':'切换');
  if(graphSession!==s)return;
  if(!accepted) {graphReturnFromPreview(s);return;}
  const r=await api('POST',s.id?`topology/${s.id}`:'topology',{graph:s.graph,path:s.prefix,name:s.name,group_id:s.group_id,port:s.port,revision:s.revision,apply:true,switch_only:!s.editor});
  await graphNavigate(r.id);toast('已下发，等待节点确认');
}

function graphReturnFromPreview(s) {
  graphSession=s.editor||null;
  if(s.editor&&s.returnView)Object.assign(topologyViews.get(s.id||'new'),s.returnView);
  drawGraph();
}

async function startGraphChoice(id,fromEditor=false) {
  const l=poolView.link,editor=fromEditor?graphSession:null;
  if(editor)graphValidateFields(editor);
  if(!editor&&(l.job||!l.active_graph||l.state==='deleting'))return toast('请先应用拓扑并等待当前操作完成',true);
  const current=!editor&&l.active_path.includes(id);
  const s={kind:'choose',id:editor?.id||l?.id,revision:editor?.revision??l?.revision,graph:structuredClone(editor?.graph||l.active_graph),
    name:editor?.name??l?.name,group_id:editor?.group_id??l?.group_id,port:editor?editor.port:l?.applied_plan?.port??null,
    prefix:current?l.active_path.slice(0,l.active_path.indexOf(id)+1):[],required:id?[id]:[],preferred:l?.active_path||[],editor,dirty:false};
  const r=await graphPreview(s,s.prefix,true,fromEditor,!editor&&!current);
  s.graph=r.graph;
  if(editor) {
    s.returnView=structuredClone(topologyBinding.view);
    s.prefix=r.path;s.complete=true;s.options=[];
    graphSession=s;drawGraph();topologyBinding?.fit();await confirmGraphChoice(s);
  } else {
    if(current&&r.options.length<2)return;
    s.prefix=r.prefix;s.complete=r.complete;s.options=r.options;
    graphSession=s;drawGraph();if(s.complete)await confirmGraphChoice(s);
  }
}

function graphPosition() {
  const g=graphSession.graph,boxes=graphBoxes(g,true),curveBoxes=boxes;
  const canvas=app.querySelector('.topology-canvas'),origin={x:Number(canvas.dataset.worldX),y:Number(canvas.dataset.worldY)};
  for(const v of g.vertices) {
    const el=app.querySelector(`[data-graph-node="${v.id}"]`);
    if(el){el.style.left=v.x-origin.x+'px';el.style.top=v.y-origin.y+'px';el.classList.toggle('is-dragging',graphDrag?.node===v);}
  }
  for(const p of g.groups) {
    const el=app.querySelector(`[data-graph-group="${p.id}"]`),b=graphBounds(g,p);
    if(el)Object.assign(el.style,{left:b.x-origin.x+'px',top:b.y-origin.y+'px',width:b.width+'px',height:b.height+'px'});
  }
  const world=graphWorld(g),svg=canvas.querySelector('.topology-cables');
  Object.assign(svg.style,{left:world.x-origin.x+'px',top:world.y-origin.y+'px'});
  svg.setAttribute('width',world.width);svg.setAttribute('height',world.height);
  svg.setAttribute('viewBox',`${world.x} ${world.y} ${world.width} ${world.height}`);
  graphConnections(g).forEach(([a,b],i)=>{
    const d=graphCurve(boxes.get(a),boxes.get(b),curveBoxes);
    app.querySelectorAll(`[data-graph-line="${i}"],[data-graph-flow="${i}"],[data-graph-hit="${i}"]`).forEach(el=>el.setAttribute('d',d));
  });
}

function graphLinkError(source,target) {
  const g=graphSession.graph,owner=id=>g.vertices.find(v=>v.id===id)?.group||id;
  source=owner(source);target=owner(target);
  if(source===target)return '不能连接组件自身';
  if(g.vertices.find(v=>v.id===source)?.kind==='target'||g.vertices.find(v=>v.id===target)?.kind==='entry')return '入口只能向外连接，目标只能接收连接';
  if(g.edges.some(([a,b])=>a===source&&b===target))return '这条连线已存在';
  const seen=new Set(),pending=[target];
  while(pending.length){const id=pending.pop();if(id===source)return '这条连线会形成循环';if(seen.has(id))continue;seen.add(id);g.edges.forEach(([a,b])=>{if(a===id)pending.push(b);});}
  return '';
}

function graphConnect(id,side) {
  const s=graphSession,g=s.graph;
  id=g.vertices.find(v=>v.id===id)?.group||id;
  if(side==='out'||!s.connect&&side!=='in') {s.connect=s.connect===id?null:id;s.edge=null;drawGraph();return;}
  if(!s.connect)return;
  const error=graphLinkError(s.connect,id);
  if(error){s.error=error;drawGraph();return;}
  g.edges.push([s.connect,id]);s.connect=null;graphTouch();drawGraph();
}

let graphWire=null;
function graphPaintConnection(point) {
  const s=graphSession,svg=app.querySelector('.topology-cables');
  if(s?.kind!=='edit'||!s.connect||!svg)return;
  const source=graphBoxes(s.graph,true).get(s.connect);if(!source)return;
  app.querySelectorAll('.graph-port').forEach(el=>{
    el.classList.toggle('graph-link-source',el.dataset.id===s.connect&&el.dataset.side==='out');
    el.classList.toggle('graph-link-target',el.dataset.side==='in'&&!graphLinkError(s.connect,el.dataset.id));
  });
  let path=svg.querySelector('.graph-wire');
  if(!path){path=document.createElementNS('http://www.w3.org/2000/svg','path');path.setAttribute('class','graph-wire');svg.append(path);}
  const canvas=app.querySelector('.topology-canvas'),rect=canvas.getBoundingClientRect(),scale=topologyBinding.view.scale;
  const x=point?(point.x-rect.left)/scale+Number(canvas.dataset.worldX):source.x+source.width+70;
  const y=point?(point.y-rect.top)/scale+Number(canvas.dataset.worldY):source.y+source.height/2;
  const sx=source.x+source.width,sy=source.y+source.height/2,bend=Math.max(40,Math.abs(x-sx)*.4);
  path.setAttribute('d',`M ${sx} ${sy} C ${sx+bend} ${sy}, ${x-bend} ${y}, ${x} ${y}`);
}

document.addEventListener('pointerdown',e=>{
  const port=e.target.closest('.graph-port.out');
  if(!port||graphSession?.kind!=='edit'||graphBusy||graphDrag||e.button!==0)return;
  e.preventDefault();e.stopPropagation();
  graphSession.connect=port.dataset.id;graphSession.error='';
  graphWire={pointer:e.pointerId,x:e.clientX,y:e.clientY};drawGraph();
  const viewport=topologyBinding.viewport;graphWire.element=viewport;
  try{viewport.setPointerCapture(e.pointerId);}catch(_){}
},true);
document.addEventListener('pointermove',e=>{
  if(graphWire&&graphWire.pointer===e.pointerId)graphWire.moved ||= Math.hypot(e.clientX-graphWire.x,e.clientY-graphWire.y)>4;
  if(graphSession?.connect)graphPaintConnection({x:e.clientX,y:e.clientY});
});
function graphEndWire(e) {
  if(!graphWire||graphWire.pointer!==e.pointerId)return;
  const wire=graphWire;graphWire=null;graphDragConsumed=true;
  try{wire.element.releasePointerCapture(e.pointerId);}catch(_){}
  if(e.type==='pointercancel'){graphSession.connect=null;drawGraph();}
  else if(wire.moved) {
    const hit=document.elementFromPoint(e.clientX,e.clientY)?.closest('[data-action]');
    if(hit&&['graph-port','graph-select','graph-group'].includes(hit.dataset.action))graphConnect(hit.dataset.id,'in');
    else {graphSession.connect=null;drawGraph();}
  }
  setTimeout(()=>graphDragConsumed=false,0);
}
document.addEventListener('pointerup',graphEndWire);document.addEventListener('pointercancel',graphEndWire);

// Pool wiring belongs to the container. A member never carries another pool's wiring into it.
function graphMembership(v,group) {
  const g=graphSession.graph,old=v.group,edges=graphConnections(g);
  if(old===group)return;
  v.group=group;
  if(group)g.edges=edges.filter(([a,b])=>a!==v.id&&b!==v.id);
  else g.edges=[...edges,...edges.filter(([a,b])=>a===old||b===old).map(([a,b])=>[a===old?v.id:a,b===old?v.id:b])];
  g.edges=graphConnections(g);
  for(const p of g.groups)if(p.locked===v.id&&p.id!==group)p.locked=null;
}

function graphDropTarget(v) {
  return [...graphSession.graph.groups].reverse().find(p=>{
    const b=graphBounds(graphSession.graph,p);
    return v.x+90>=b.x&&v.x+90<=b.x+b.width&&v.y+30>=b.y+32&&v.y+30<=b.y+b.height;
  });
}

function graphDropError(v,p) {
  if(!p)return '';
  if(!['node','direct'].includes(v.kind))return '入口和目标不能加入池';
  const n=poolView.nodes.find(n=>n.id===v.node_id);
  if(v.kind!=='direct'&&!n?.eligible)return n?poolMemberReason(n):'节点不存在';
  if(v.group!==p.id&&graphSession.graph.vertices.some(n=>n.group===p.id&&(v.kind==='direct'?n.kind==='direct':n.node_id===v.node_id)))return '池中已有该节点';
  if(v.group!==p.id&&graphSession.graph.vertices.filter(n=>n.group===p.id).length>=32)return '池最多包含 32 个成员';
  return '';
}

function graphFitMember(v,p,joining) {
  const others=graphSession.graph.vertices.filter(n=>n!==v&&n.group===p.id);
  if(joining)for(let tries=0;tries<=others.length;tries++) {
    const overlap=others.find(n=>v.x<n.x+188&&v.x+188>n.x&&v.y<n.y+68&&v.y+68>n.y);
    if(!overlap)break;
    v.y=overlap.y+76;
  }
  // Enlarge only after a drop, never while a member is being dragged out.
  const right=Math.max(p.x+p.width,v.x+200),bottom=Math.max(p.y+p.height,v.y+84);
  p.x=Math.min(p.x,v.x-20);p.y=Math.min(p.y,v.y-44);
  p.width=right-p.x;p.height=bottom-p.y;
}

let graphDrag=null;
document.addEventListener('pointerdown',e=>{
  const el=e.target.closest('[data-graph-drag]');
  if(!el||graphDrag||graphSession?.kind!=='edit'||graphBusy||e.button!==0||graphSession.connect)return;
  e.preventDefault();e.stopPropagation();
  const g=graphSession.graph,group=el.dataset.graphDrag!=='node',resize=el.dataset.graphDrag==='resize';
  const p=group?g.groups.find(p=>p.id===el.dataset.id):null;
  const items=group?[p,...(resize?[]:g.vertices.filter(v=>v.group===p.id))]:g.vertices.filter(v=>v.id===el.dataset.id);
  graphSession.selected=el.dataset.id;graphSession.edge=null;
  graphDrag={x:e.clientX,y:e.clientY,scale:topologyBinding.view.scale,points:items.map(v=>({v,x:v.x,y:v.y,...(v.width?{width:v.width,height:v.height}:{})})),pointer:e.pointerId,resize,
    node:group?null:items[0],element:el,dirty:graphSession.dirty,viewX:topologyBinding.view.x,viewY:topologyBinding.view.y};
  try{el.setPointerCapture(e.pointerId);}catch(_){} // Synthetic test events have no active pointer.
},true);
function graphMoveDrag(x,y) {
  const drag=graphDrag;if(!drag)return;
  drag.clientX=x;drag.clientY=y;
  const view=topologyBinding.view;
  const dx=(x-drag.x-(view.x-drag.viewX))/drag.scale,dy=(y-drag.y-(view.y-drag.viewY))/drag.scale;
  if(Math.abs(dx)+Math.abs(dy)<4&&!drag.moved)return;
  drag.moved=true;graphDragConsumed=true;graphTouch(false);view.auto=false;
  if(drag.resize) {
    const original=drag.points[0],p=original.v,members=graphSession.graph.vertices.filter(v=>v.group===p.id);
    p.width=Math.round(Math.max(220,original.width+dx,...members.map(v=>v.x+200-p.x)));
    p.height=Math.round(Math.max(144,original.height+dy,...members.map(v=>v.y+84-p.y)));
  } else for(const p of drag.points){p.v.x=Math.round(p.x+dx);p.v.y=Math.round(p.y+dy);}
  const v=drag.node,target=v?graphDropTarget(v):null;
  app.querySelectorAll('[data-graph-group]').forEach(el=>{
    const over=el.dataset.graphGroup===target?.id;
    el.classList.toggle('graph-drop-target',over&&!graphDropError(v,target));
    el.classList.toggle('graph-drop-blocked',over&&!!graphDropError(v,target));
  });
  graphPosition();
}

function graphPanWhileDragging() {
  const drag=graphDrag;if(!drag)return;
  drag.frame=null;if(!drag.moved)return;
  const r=topologyBinding.viewport.getBoundingClientRect(),x=drag.clientX,y=drag.clientY;
  if(x<r.left-60||x>r.right+60||y<r.top-60||y>r.bottom+60)return;
  const speed=(p,min,max)=>p<min+36?Math.min(10,(min+36-p)/4):p>max-36?-Math.min(10,(p-max+36)/4):0;
  const dx=speed(x,r.left,r.right),dy=speed(y,r.top,r.bottom);
  if(!dx&&!dy)return;
  topologyBinding.view.x+=dx;topologyBinding.view.y+=dy;topologyBinding.apply();
  graphMoveDrag(x,y);drag.frame=requestAnimationFrame(graphPanWhileDragging);
}

document.addEventListener('pointermove',e=>{
  if(!graphDrag||e.pointerId!==graphDrag.pointer)return;
  graphMoveDrag(e.clientX,e.clientY);
  if(!graphDrag.frame)graphDrag.frame=requestAnimationFrame(graphPanWhileDragging);
});

const endGraphDrag=e=>{
  if(!graphDrag||e.pointerId!==graphDrag.pointer)return;
  const drag=graphDrag,cancelled=e.type==='pointercancel',v=drag.node;
  if(drag.frame)cancelAnimationFrame(drag.frame);
  if(drag.moved) {
    const target=v?graphDropTarget(v):null,error=v?graphDropError(v,target):'';
    if(cancelled||error) {
      for(const point of drag.points)Object.assign(point.v,{x:point.x,y:point.y,...(point.width?{width:point.width,height:point.height}:{})});
      graphSession.dirty=drag.dirty;
      if(error&&!cancelled)toast(error,true);
    } else if(v&&['node','direct'].includes(v.kind)) {
      const group=target?.id||null,joining=group!==v.group;
      if(joining)graphMembership(v,group);
      if(target)graphFitMember(v,target,joining);
      for(const p of graphSession.graph.groups)if(p.locked===v.id&&p.id!==group)p.locked=null;
    }
  }
  graphDrag=null;
  try{drag.element.releasePointerCapture(e.pointerId);}catch(_){}
  if(drag.moved){graphTouch();drawGraph();}else graphButtons();
  setTimeout(()=>graphDragConsumed=false,0);
};
document.addEventListener('pointerup',endGraphDrag);document.addEventListener('pointercancel',endGraphDrag);
document.addEventListener('wheel',e=>{if(graphDrag){e.preventDefault();e.stopPropagation();}},{capture:true,passive:false});

document.addEventListener('input',e=>{
  if(graphSession?.kind!=='edit'||graphBusy)return;
  const field=e.target.dataset.graphField;
  if(field){graphSession[field]=field==='name'?e.target.value:e.target.value===''?null:Number(e.target.value);graphTouch(false);}
  if(e.target.hasAttribute('data-graph-group-name')){graphSession.graph.groups.find(g=>g.id===graphSession.selected).name=e.target.value;graphTouch(false);const title=app.querySelector(`[data-graph-group="${graphSession.selected}"] .graph-pool-title`);if(title)title.textContent=e.target.value;}
});
document.addEventListener('change',e=>{
  if(graphSession?.kind!=='edit'||graphBusy)return;
  if(e.target.hasAttribute('data-graph-field')||e.target.hasAttribute('data-graph-group-name'))graphTouch();
  if(e.target.id==='graph-add-kind'){graphAddDialog(e.target.value);return;}
  const prop=e.target.dataset.graphProperty,v=graphSession.graph.vertices.find(v=>v.id===graphSession.selected);
  if(e.target.hasAttribute('data-graph-resource')&&v) {
    const id=Number(e.target.value);
    if(v.group&&graphSession.graph.vertices.some(n=>n!==v&&n.group===v.group&&n.node_id===id)){toast('池中已有该节点',true);drawGraph();return;}
    v[v.kind==='target'?'target_id':'node_id']=id;graphTouch();drawGraph();
  }
  if(prop&&v){if(prop==='group')graphMembership(v,e.target.value||null);else v[prop]=e.target.checked;for(const g of graphSession.graph.groups)if(g.locked===v.id&&(v.disabled||v.group!==g.id))g.locked=null;graphTouch();drawGraph();}
  if(e.target.hasAttribute('data-graph-lock')){graphSession.graph.groups.find(g=>g.id===graphSession.selected).locked=e.target.value||null;graphTouch();drawGraph();}
});

document.addEventListener('keydown',e=>{
  if(graphSession?.kind!=='edit'||!$('#modal').hidden)return;
  if(e.key==='Escape'&&graphDrag){e.preventDefault();e.stopImmediatePropagation();endGraphDrag({pointerId:graphDrag.pointer,type:'pointercancel'});return;}
  if(e.key==='Escape'&&graphWire){e.preventDefault();e.stopImmediatePropagation();graphEndWire({pointerId:graphWire.pointer,type:'pointercancel'});return;}
  if(e.target.closest('input,select,textarea,[contenteditable="true"]')||graphBusy)return;
  if((e.ctrlKey||e.metaKey)&&['z','y'].includes(e.key.toLowerCase())){e.preventDefault();graphUndo(e.shiftKey||e.key.toLowerCase()==='y');return;}
  if(e.key==='Escape'&&(graphSession.connect||graphSession.selected||graphSession.edge!=null)) {e.preventDefault();e.stopImmediatePropagation();graphSession.connect=null;graphSession.selected=null;graphSession.edge=null;drawGraph();}
  if(['Delete','Backspace'].includes(e.key)&&!graphDrag) {
    e.preventDefault();const s=graphSession;
    if(s.edge!=null)ACTIONS['graph-remove-edge']();
    else if(s.graph.groups.some(p=>p.id===s.selected))ACTIONS['graph-remove-group']();
    else if(s.selected)ACTIONS['graph-remove']();
  }
},true);
document.addEventListener('click',e=>{
  if(graphSession?.kind!=='edit'||graphBusy||graphDragConsumed||!e.target.closest('.graph-viewport')||e.target.closest('[data-action]'))return;
  graphSession.connect=null;graphSession.selected=null;graphSession.edge=null;drawGraph();
});
window.addEventListener('blur',()=>{
  if(graphDrag)endGraphDrag({pointerId:graphDrag.pointer,type:'pointercancel'});
  if(graphWire)graphEndWire({pointerId:graphWire.pointer,type:'pointercancel'});
});

Object.assign(ACTIONS,{
  'link-new':()=>{location.hash='#/routes/new';},
  'link-edit':()=>startGraphEditor(poolView.link),
  'graph-actual-size':()=>topologyBinding?.zoom(1/topologyBinding.view.scale),
  'graph-undo':()=>graphUndo(),
  'graph-redo':()=>graphUndo(true),
  'graph-add-open':el=>graphAddDialog(el?.dataset.kind),
  'graph-add-node':()=>{
    const s=graphSession,kind=$('#graph-add-kind').value,id=Number($('#graph-add-resource').value);if(!id)return;
    if(kind!=='node'&&s.graph.vertices.some(v=>v.kind===kind))return toast('只能添加一个'+(kind==='entry'?'入口':'目标'),true);
    const v={id:graphID(),kind,[kind==='target'?'target_id':'node_id']:id,group:null,disabled:false,...graphAddPosition(180,60)};
    s.graph.vertices.push(v);s.selected=v.id;s.edge=null;graphTouch();closeModal();drawGraph();topologyBinding?.fit();
  },
  'graph-add-group':()=>{const name=$('#graph-pool-name').value.trim();if(!name)return;const g={id:graphID(),name,locked:null,...graphAddPosition(260,180),width:260,height:180};graphSession.graph.groups.push(g);graphSession.selected=g.id;graphSession.edge=null;graphTouch();closeModal();drawGraph();topologyBinding?.fit();},
  'graph-group':el=>{if(graphDragConsumed||graphSession?.kind!=='edit'||graphBusy)return;if(graphSession.connect)return graphConnect(el.dataset.id,'in');graphSession.selected=el.dataset.id;graphSession.edge=null;drawGraph();},
  'graph-port':el=>{if(graphDragConsumed||graphSession?.kind!=='edit'||graphBusy)return;graphConnect(el.dataset.id,el.dataset.side);},
  'graph-select':el=>{
    if(graphDragConsumed||graphBusy)return;
    const s=graphSession,id=el.dataset.id;
    if(!s)return graphWork(()=>startGraphChoice(id));
    if(s.kind==='choose') {
      if(s.options.includes(id)){s.error='';return graphWork(()=>advanceGraph(s,true,[...s.prefix,id]));}
      if(s.prefix.includes(id)&&!s.complete)return ACTIONS['graph-rewind'](el);
      return graphWork(()=>startGraphChoice(id));
    }
    if(s.connect)return graphConnect(id,'in');
    s.selected=id;s.edge=null;drawGraph();
  },
  'graph-edge':el=>{if(graphSession?.kind!=='edit'||graphBusy)return;graphSession.edge=Number(el.dataset.index);graphSession.selected=null;drawGraph();},
  'graph-remove':()=>{const s=graphSession,id=s.selected;s.graph.vertices=s.graph.vertices.filter(v=>v.id!==id);s.graph.edges=s.graph.edges.filter(([a,b])=>a!==id&&b!==id);for(const p of s.graph.groups)if(p.locked===id)p.locked=null;s.selected=null;s.connect=null;graphTouch();drawGraph();},
  'graph-remove-group':()=>{
    const s=graphSession,id=s.selected,members=s.graph.vertices.filter(v=>v.group===id).map(v=>v.id);
    s.graph.edges=s.graph.edges.flatMap(([a,b])=>(a===id?members:[a]).flatMap(x=>(b===id?members:[b]).map(y=>[x,y])));
    s.graph.vertices.forEach(v=>{if(v.group===id)v.group=null;});s.graph.groups=s.graph.groups.filter(g=>g.id!==id);s.selected=null;s.connect=null;graphTouch();drawGraph();
  },
  'graph-remove-edge':()=>{graphSession.graph.edges.splice(graphSession.edge,1);graphSession.edge=null;graphTouch();drawGraph();},
  'graph-check':()=>graphWork(async()=>{const s=graphSession,r=await graphPreview(s,[],false,true);s.graph=r.graph;graphTouch();s.notice='校验通过';drawGraph();topologyBinding?.fit();}),
  'graph-save-draft':()=>graphWork(async()=>{const s=graphSession;graphValidateFields(s);const r=await api('POST',s.id?`topology/${s.id}`:'topology',{graph:s.graph,name:s.name,group_id:s.group_id,port:s.port,revision:s.revision,apply:false});await graphNavigate(r.id);toast('草稿已保存');}),
  'graph-publish':()=>graphWork(()=>startGraphChoice(null,true)),
  'graph-rewind':el=>graphWork(async()=>{const s=graphSession,index=s.prefix.indexOf(el.dataset.id);if(index<0)return;s.prefix=s.prefix.slice(0,index+1);s.complete=false;s.error='';await advanceGraph(s,false);}),
  'graph-confirm':()=>graphWork(()=>confirmGraphChoice(graphSession)),
  'graph-reload':()=>graphWork(async()=>{
    const s=graphSession,editing=s.kind==='edit'||!!s.editor;
    if((s.dirty||s.editor?.dirty)&&!await confirmBox('重新载入？','将放弃当前未保存的编辑，载入服务端最新状态。','载入'))return;
    const data=await api('GET',`links/${s.id}`);poolView=data;
    if(editing&&!data.link.job&&data.link.state!=='deleting')startGraphEditor(data.link);
    else{graphSession=null;drawGraph();}
  }),
  'graph-exit':async()=>{
    if(graphBusy||graphDrag)return;
    const s=graphSession;
    if(s?.kind==='choose'&&s.editor){graphReturnFromPreview(s);return;}
    if(s?.dirty&&!await confirmBox('放弃当前编辑？','未保存的修改将被丢弃。','放弃'))return;
    graphSession=null;if(!s?.id)location.hash='#/routes';else await refresh();
  }
});
