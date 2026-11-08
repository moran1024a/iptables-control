// Editing saves structure; background jobs confirm the forwarding configuration.
let graphSession = null;
let graphBusy = false;
let graphDragConsumed = false;
const graphID = () => crypto.randomUUID?.() || `v-${Date.now()}-${Math.random().toString(16).slice(2)}`;
const graphChanged = () => graphBusy || !!graphSession?.dirty || !!graphSession?.editor?.dirty;
const graphName = v => {
  if(!v)return '未知节点';
  if(v.kind==='direct')return 'direct';
  const name=resourceName(v.kind==='target'?canvasView.targets:canvasView.nodes,v.target_id||v.node_id);
  return name;
};
const graphLabel = v => {
  const node=v?.node_id&&canvasView.nodes.find(n=>n.id===v.node_id);
  return node?nodeLabel(node):esc(graphName(v));
};
const graphCurrent = () => graphSession?.graph || canvasView.link.view_graph || canvasView.link.graph;
const graphPairs = path => new Set((path||[]).slice(1).map((b,i)=>`${path[i]}/${b}`));

async function graphWork(work) {
  if (graphBusy || graphDrag) return;
  const session=graphSession,page=pageVersion;
  graphBusy = true;
  graphButtons();
  try { return await work(); }
  catch(e) {
    if(page!==pageVersion||e instanceof ApiError&&graphSession!==session&&graphSession?.editor!==session)return;
    if(graphSession?.editor)graphReturnFromPreview(graphSession);
    if(graphSession) {
      graphSession.error=e.message;graphSession.errorField=e.field||'';graphSession.conflict=e.status===409;
      if(graphSession.kind==='edit'&&e.field?.startsWith('vertex:')){graphSession.selected=e.field.slice(7);graphSession.edge=null;}
      drawGraph();
      if(graphSession.kind==='edit'&&e.field?.startsWith('vertex:')){
        const vertex=graphSession.graph.vertices.find(v=>v.id===e.field.slice(7));if(vertex)graphReveal(vertex);
      }
    }
    else toast(e.message,true);
  } finally {
    graphBusy=false;
    graphButtons();
  }
}

async function graphNavigate(id) {
  graphSession=null;
  const hash=`#/routes/${id}`;
  if(location.hash===hash)await refresh();
  else {graphBusy=false;location.hash=hash;}
}

function graphWorld(g) {
  const boxes=g.vertices.map(v=>({...v,width:graphWidth(v),height:graphHeight(v,g)}));
  const x=boxes.length?Math.min(...boxes.map(b=>b.x-60)):0,y=boxes.length?Math.min(...boxes.map(b=>b.y-100)):0;
  return {x,y,width:Math.max(700,...boxes.map(b=>b.x+b.width+60-x)),height:Math.max(320,...boxes.map(b=>b.y+b.height+60-y))};
}

function graphAddPosition(width,height) {
  const canvas=app.querySelector('.topology-canvas'),view=topologyBinding.view,viewport=topologyBinding.viewport;
  let x=Math.round((viewport.clientWidth/2-view.x)/view.scale+Number(canvas.dataset.worldX)-width/2);
  let y=Math.round((viewport.clientHeight/2-view.y)/view.scale+Number(canvas.dataset.worldY)-height/2);
  const g=graphSession.graph,boxes=g.vertices.map(v=>({...v,width:graphWidth(v),height:graphHeight(v,g)}));
  // Add beside existing nodes without covering them.
  for(let tries=0;tries<=boxes.length;tries++) {
    const collision=boxes.find(b=>x<b.x+b.width+20&&x+width>b.x-20&&y<b.y+b.height+20&&y+height>b.y-20);
    if(!collision)break;
    x=collision.x+collision.width+40;
  }
  return {x,y};
}

function graphConnections(g) { return [...new Map(g.edges.map(e=>[e.join('/'),e])).values()]; }

function graphAddresses(id,g=graphCurrent()) {
  const v=g.vertices.find(v=>v.id===id);
  const resource=v?.kind==='target'?canvasView.targets.find(n=>n.id===v.target_id):canvasView.nodes.find(n=>n.id===v?.node_id);
  return g.version===2?(resource?.addresses||Object.values(g.address_book?.[id]||{})):[];
}
const graphWidth=v=>v.kind==='direct'?110:220;
const graphWeight=n=>Number(n)>0?'+'+Number(n):String(Number(n)||0);
const graphHeight=(v,g)=>v.kind==='direct'?56:62+24*(Math.max(1,graphAddresses(v.id,g).length)+(g.routing_version===2&&v.node_id?1:0));
function graphDisplay(g) {
  if(graphSession?.kind==='edit'||graphSession?.editor)return g;
  const view=canvasView.link?.view_graph;if(!view)return g;
  return {...g,vertices:g.vertices.map(v=>({...v,...Object.fromEntries(['x','y'].map(k=>[k,view.vertices.find(n=>n.id===v.id)?.[k]??v[k]]))}))};
}
function graphRenderEdges(g) { return graphConnections(g); }
function graphBoxes(g) {
  return new Map(g.vertices.map(v=>[v.id,{...v,width:graphWidth(v),height:graphHeight(v,g)}]));
}
function graphPortAddress(g,v,side,edge=null) {
  if(v.kind==='entry'&&side==='in')return v.address||'';
  const key=side==='in'?'to':'from',owner=v.id;
  if(edge)return g.bindings?.[edge.join('/')]?.[key]?.[v.id]||(v.kind==='target'?'main':'');
  const values=graphConnections(g).filter(e=>e[side==='in'?1:0]===owner).map(e=>g.bindings?.[e.join('/')]?.[key]?.[v.id]).filter(Boolean);
  if(values.length&&new Set(values).size===1)return values[0];
  return v[side+'_address']||(v.kind==='target'?'main':g.routing_version===2&&side==='out'?'@auto':graphAddresses(v.id,g).length===1?graphAddresses(v.id,g)[0].id:'');
}
function graphPortPoint(g,id,side,address,boxes) {
  const b=boxes.get(id),v=g.vertices.find(v=>v.id===id),addresses=graphAddresses(id,g);
  if(v?.kind==='direct')return [side==='out'?b.x+b.width:b.x,b.y+b.height/2];
  const i=address==='@auto'?Math.max(1,addresses.length):addresses.findIndex(a=>a.id===address);
  return [side==='out'?b.x+b.width:b.x,b.y+(v?(i>=0?62+i*24+12:62+12):b.height/2)];
}
function graphEdgeCurve(g,a,b,boxes,index=0) {
  const source=a,target=b;
  const va=g.vertices.find(v=>v.id===source),vb=g.vertices.find(v=>v.id===target);
  const start=graphPortPoint(g,source,'out',va?graphPortAddress(g,va,'out',[a,b]):null,boxes);
  const end=graphPortPoint(g,target,'in',vb?graphPortAddress(g,vb,'in',[a,b]):null,boxes);
  const outerA=boxes.get(a),outerB=boxes.get(b);
  const barriers=[...boxes.values()];
  return graphRoute(start,end,outerA,outerB,barriers,index);
}
function graphPathBlocked(points,boxes,padding=12) {
  return points.slice(1).some((q,i)=>boxes.some(b=>{
    const p=points[i],lo=[b.x-padding,b.y-padding],hi=[b.x+b.width+padding,b.y+b.height+padding];
    let start=0,end=1;
    for(let axis=0;axis<2;axis++){
      const delta=q[axis]-p[axis];
      if(!delta){if(p[axis]<=lo[axis]||p[axis]>=hi[axis])return false;continue;}
      const a=(lo[axis]-p[axis])/delta,c=(hi[axis]-p[axis])/delta;
      start=Math.max(start,Math.min(a,c));end=Math.min(end,Math.max(a,c));if(start>=end)return false;
    }
    return true;
  }));
}
function graphRoundedPath(points) {
  const p=[];
  for(const v of points){
    if(p.length&&p.at(-1)[0]===v[0]&&p.at(-1)[1]===v[1])continue;
    while(p.length>1&&(p.at(-2)[0]===p.at(-1)[0]&&p.at(-1)[0]===v[0]||p.at(-2)[1]===p.at(-1)[1]&&p.at(-1)[1]===v[1]))p.pop();
    p.push(v);
  }
  const xy=v=>v.map(n=>Math.round(n*10)/10).join(' ');
  let d=`M ${xy(p[0])}`;
  for(let i=1;i<p.length-1;i++){
    const a=p[i-1],b=p[i],c=p[i+1],before=Math.hypot(b[0]-a[0],b[1]-a[1]),after=Math.hypot(c[0]-b[0],c[1]-b[1]),r=Math.min(8,before/2,after/2);
    d+=` L ${xy(b.map((n,j)=>n+(a[j]-n)*r/before))} Q ${xy(b)} ${xy(b.map((n,j)=>n+(c[j]-n)*r/after))}`;
  }
  return d+` L ${xy(p.at(-1))}`;
}
function graphRoutePoints(start,end,source,target,boxes,index=0,used=[],clearance=6,bendOffset=null) {
  const penalty=(a,b)=>graphLanePenalty(a,b,used,clearance);
  const room=end[0]-start[0],gap=room>0?Math.min(24+(index%5)*8,room/3):24+(index%5)*8;
  const exit=[start[0]+gap,start[1]],enter=[end[0]-gap,end[1]];
  const obstacles=boxes.filter(b=>b.id!==source.id&&b.id!==target.id);
  if(end[0]>start[0]&&start[1]===end[1]&&!graphPathBlocked([start,end],obstacles)&&penalty(start,end)===0)return [start,end];
  if(enter[0]>=exit[0]){
    const x=Math.max(exit[0],Math.min(enter[0],(exit[0]+enter[0])/2+(bendOffset??((index%5)-2)*8))),p=[start,[x,start[1]],[x,end[1]],end];
    if(!graphPathBlocked(p,obstacles)&&p.slice(1).every((b,i)=>penalty(p[i],b)===0))return p;
  }
  // Search a small orthogonal visibility grid; a turn costs more than a short detour.
  const xs=[...new Set([exit[0],enter[0],...boxes.flatMap(b=>[b.x-gap,b.x+b.width+gap]),...used.flatMap(([a,b])=>a[0]===b[0]?[a[0]-8,a[0]+8]:[])])].sort((a,b)=>a-b);
  const ys=[...new Set([exit[1],enter[1],...boxes.flatMap(b=>[b.y-gap,b.y+b.height+gap]),...used.flatMap(([a,b])=>a[1]===b[1]?[a[1]-8,a[1]+8]:[])])].sort((a,b)=>a-b);
  const sx=xs.indexOf(exit[0]),sy=ys.indexOf(exit[1]),tx=xs.indexOf(enter[0]),ty=ys.indexOf(enter[1]);
  const key=(x,y,d)=>`${x},${y},${d}`,first={x:sx,y:sy,dir:0,cost:0,prev:null};
  const open=[first],costs=new Map([[key(sx,sy,0),0]]);let found=null,visits=0;
  while(open.length&&visits++<12000){
    let best=0;for(let i=1;i<open.length;i++)if(open[i].rank<open[best].rank)best=i;
    const n=open.splice(best,1)[0];
    if(n.x===tx&&n.y===ty){found=n;break;}
    for(const [dx,dy,dir] of [[-1,0,0],[1,0,0],[0,-1,1],[0,1,1]]){
      const x=n.x+dx,y=n.y+dy;if(x<0||y<0||x>=xs.length||y>=ys.length)continue;
      const a=[xs[n.x],ys[n.y]],b=[xs[x],ys[y]];
      if(graphPathBlocked([a,b],boxes))continue;
      const cost=n.cost+Math.abs(a[0]-b[0])+Math.abs(a[1]-b[1])+(dir!==n.dir?38:0)+penalty(a,b),k=key(x,y,dir);
      if(cost>=(costs.get(k)??Infinity))continue;
      costs.set(k,cost);open.push({x,y,dir,cost,prev:n,rank:cost+Math.abs(xs[x]-enter[0])+Math.abs(ys[y]-enter[1])});
    }
  }
  if(found){const path=[];for(let n=found;n;n=n.prev)path.push([xs[n.x],ys[n.y]]);return [start,...path.reverse(),end];}
  const y=Math.min(...boxes.map(b=>b.y))-gap;
  return [start,exit,[exit[0],y],[enter[0],y],enter,end];
}
function graphRoute(...args) {return graphRoundedPath(graphRoutePoints(...args));}
function graphCurve(a,b,boxes) {return graphRoute([a.x+a.width,a.y+a.height/2],[b.x,b.y+b.height/2],a,b,[...boxes.values()]);}


function graphFocusWires(event) {
  // Removing a focused element fires focusout inside render's DOM update.
  if(rendering)return;
  const canvas=app.querySelector('.graph-viewport .topology-canvas');if(!canvas)return;
  const leaving=event.type==='mouseout'||event.type==='focusout';
  const target=leaving?event.relatedTarget:event.target;
  if(graphSession?.kind==='edit'){
    graphFocusEditor(target instanceof Element?target:null);return;
  }
  const item=target instanceof Element?target.closest('[data-graph-node]'):null;
  const inPreview=target instanceof Element&&target.closest('[data-failover-popover]');
  const id=item?.dataset.graphNode||(inPreview?graphFailurePreview?.id:'')||'';
  graphQuickHint(id);
  if(canvas.dataset.graphFocus===id)return;
  canvas.dataset.graphFocus=id;canvas.classList.toggle('graph-focus',!!id);
  const segments=new Set(graphRoutes(canvasView.link).filter(r=>r.path.includes(id)).flatMap(r=>
    r.path.slice(1).map((b,i)=>JSON.stringify([r.entry,r.path[i],b]))));
  canvas.querySelectorAll('[data-graph-from]').forEach(path=>path.classList.toggle('graph-nearby',path.dataset.routeEntry?
    segments.has(JSON.stringify([path.dataset.routeEntry,path.dataset.graphFrom,path.dataset.graphTo])):
    path.dataset.graphFrom===id||path.dataset.graphTo===id));
}
for(const event of ['mouseover','mouseout','focusin','focusout'])document.addEventListener(event,graphFocusWires);

function graphFocusEditor(target=null) {
  if(graphSession?.kind!=='edit')return;
  const s=graphSession,canvas=app.querySelector('.topology-canvas');if(!canvas)return;
  const hover=target?.closest('[data-graph-node]')?.dataset.graphNode;
  const line=target?.closest('[data-graph-hit]')?.dataset.graphHit;
  const node=s.connectTarget||s.connect||hover||(line==null?s.selected:null);
  const edge=line!=null?Number(line):s.edge;
  const focused=!!node||edge!=null;
  canvas.classList.toggle('graph-edit-focus',focused);
  canvas.querySelectorAll('[data-graph-from]').forEach(el=>{
    el.classList.toggle('graph-nearby',!!(node?(el.dataset.graphFrom===node||el.dataset.graphTo===node):edge!=null&&Number(el.dataset.edgeIndex)===edge));
  });
}


function graphPort(id,side,address='',top=null,listen=false,selected=false) {
  return `<button class="graph-port ${side} ${selected?'is-bound':''}" data-action="${listen?'graph-listen':'graph-port'}" data-side="${side}" data-id="${esc(id)}" data-address="${esc(address)}" ${top==null?'':`style="top:${top}px"`} title="${side==='in'?'接收 IP':'发出 IP'}" aria-label="${side==='in'?'接收':'发出'} ${esc(address)}">${side==='in'?'‹':'›'}</button>`;
}
function graphNodeAddresses(g,v,editing) {
  if(v.kind==='direct')return editing?['in','out'].map(side=>`<button class="graph-port ${side}" data-action="graph-port" data-side="${side}" data-id="${esc(v.id)}" data-address="" style="top:28px" aria-label="${side==='in'?'连入':'连出'} direct">${side==='in'?'‹':'›'}</button>`).join(''):'';
  const node=canvasView.nodes.find(n=>n.id===v.node_id),target=canvasView.targets.find(n=>n.id===v.target_id),addresses=graphAddresses(v.id,g);
  const edge=editing&&graphSession.edge!=null?g.edges[graphSession.edge]:null;
  const incoming=graphPortAddress(g,v,'in',edge),outgoing=graphPortAddress(g,v,'out',edge);
  const currentIn=new Set(),currentOut=new Set();
  if(!editing)for(const route of graphRoutes(canvasView.link).filter(r=>!r.pending)){
    const index=route.path.indexOf(v.id),graph=route.graph||g,vertex=graph.vertices.find(n=>n.id===v.id);
    if(index<0||!vertex)continue;
    currentIn.add(graphPortAddress(graph,vertex,'in',index>0?[route.path[index-1],v.id]:null));
    if(index<route.path.length-1)currentOut.add(graphPortAddress(graph,vertex,'out',[v.id,route.path[index+1]]));
  }
  const rows=addresses.length?addresses:[{id:'',name:v.kind==='target'?'':'主 IP',host:node?.addr||target?.host||''}];
  const rendered=rows.map((a,i)=>{
    return `<div class="graph-ip-row" data-graph-address="${esc(a.id)}" ${editing?`data-action="graph-receive" data-id="${esc(v.id)}" data-address="${esc(a.id)}"`:''} title="${esc(a.host)}${a.port?':'+a.port:''}"><span class="graph-ip-name">${esc(a.name||(a.primary?'主 IP':'IP'))}</span><code>${esc(a.host)}${a.port?':'+a.port:target?.port?':'+target.port:''}</code>${editing?`${graphPort(v.id,'in',a.id,74+i*24,v.kind==='entry',a.id===incoming)}${v.kind==='target'?'':graphPort(v.id,'out',a.id,74+i*24,false,a.id===outgoing)}`:`<span class="graph-ip-role">${a.id&&currentIn.has(a.id)?'入':''}${a.id&&currentOut.has(a.id)?' 出':''}</span>`}</div>`;
  }).join('');
  if(g.routing_version!==2||!v.node_id)return rendered;
  return rendered+`<div class="graph-ip-row graph-auto-out"><span>自动出口</span>${editing?graphPort(v.id,'out','@auto',74+rows.length*24,false,outgoing==='@auto'):`<span class="graph-ip-role">${currentOut.has('@auto')?'出':''}</span>`}</div>`;
}
function graphCanvas(raw, l) {
  const g=graphDisplay(raw),editing=graphSession?.kind==='edit',choosing=graphSession?.kind==='choose';
  const pairs=paths=>new Set(paths.flatMap(p=>[...graphPairs(p)]));
  const routes=graphRoutes(l),activePaths=routes.filter(r=>!r.pending).map(r=>r.path);
  const active=pairs(activePaths),pending=pairs([...routes.filter(r=>r.pending).map(r=>r.path),...(choosing?[graphSession.prefix]:[])]);
  const on=new Set(activePaths.flat()),options=new Set(choosing?graphSession.options:[]),chosen=new Set(choosing?graphSession.prefix:[]);
  const boxes=graphBoxes(g),edges=graphRenderEdges(g),world=graphWorld(g),{width,height}=world;
  const cards=g.vertices.map(v=>{
    const n=canvasView.nodes.find(n=>n.id===v.node_id),current=on.has(v.id),candidate=options.has(v.id),selected=graphSession?.selected===v.id;
    const unavailable=false;
    const badge=editing?(graphSession.connectTarget===v.id?'选择接收 IP':v.kind==='entry'&&!v.address?'选择入口 IP':''):candidate?'选下一跳':chosen.has(v.id)?'已选':current?graphEntryMarks(g,l,v.id):'';
    const tag=!editing&&v.kind==='target'?'div':'button';
    const role=v.runtime_only?'旧路径':v.kind==='direct'?'direct':v.kind==='entry'?'入口':v.kind==='target'?'目标':n?.mode==='passive'?'被动节点':'节点';
    return `<div class="graph-node ${v.kind==='direct'?'graph-direct':''} ${current?'on-route':''} ${candidate?'graph-option':''} ${chosen.has(v.id)?'graph-chosen':''} ${selected?'graph-selected':''} ${unavailable?'graph-disabled':''} ${graphSession?.errorField===`vertex:${v.id}`?'graph-invalid':''}" data-key="graph-node-${esc(v.id)}" data-graph-node="${esc(v.id)}" ${current?'data-current':''} style="left:${v.x-world.x}px;top:${v.y-world.y}px;width:${graphWidth(v)}px;height:${graphHeight(v,g)}px"><${tag} class="graph-node-body" ${tag==='button'?`data-action="graph-select" data-id="${esc(v.id)}"`:''} ${editing?'data-graph-drag="node"':unavailable?'disabled':''}><small><span class="graph-node-meta"><span>${role}</span>${nodeTags(n?.tags,2,false)}</span><span class="graph-node-badge">${badge}</span></small><strong title="${esc(n?nodeNameText(n):graphName(v))}">${v.kind==='direct'?graphWeight(v.weight):esc(graphName(v))}${v.node_id?`<em class="graph-weight" title="全局权重">${graphWeight(n?.weight??v.weight)}</em>`:''}</strong></${tag}>${graphNodeAddresses(g,v,editing)}</div>`;
  }).join('');
  const order=({a,b,i})=>(active.has(`${a}/${b}`)?1:0)+(pending.has(`${a}/${b}`)?2:0)+(editing&&graphSession?.edge===i?4:0);
  const editorRoutes=graphConnectionRoutes(g,boxes,editing);
  let lines=edges.map(([a,b],i)=>({a,b,i})).sort((a,b)=>order(a)-order(b)).map(({a,b,i})=>{
    if(!boxes.has(a)||!boxes.has(b))return '';
    const key=`${a}/${b}`,d=editorRoutes[i].d;
    const attrs=`data-graph-from="${esc(a)}" data-graph-to="${esc(b)}" data-edge-index="${i}"`;
    const marker=editing?`graph-edit-arrow-${i}`:'graph-arrow';
    const label=editing?edges[i].map((id,n)=>{const v=g.vertices.find(v=>v.id===id),address=graphPortAddress(g,v,n?'in':'out',[a,b]),ip=graphAddresses(id,g).find(a=>a.id===address);return graphName(v)+(ip?' · '+ip.host:'');}).join(' → '):'';
    return `${editing?`<defs><marker id="${marker}" viewBox="0 0 6 6" refX="6" refY="3" markerUnits="userSpaceOnUse" markerWidth="6" markerHeight="${editorRoutes[i].arrowHeight}" preserveAspectRatio="none" orient="auto"><path d="M 0 0 L 6 3 L 0 6 Z" fill="context-stroke"/></marker></defs><path ${attrs} data-graph-halo="${i}" class="graph-edit-halo" d="${d}"/>`:''}<path data-graph-line="${i}" ${attrs} class="graph-line ${!editing?'graph-context':''} ${!editing&&(active.has(key)||pending.has(key))?'graph-covered':''} ${editing&&graphSession?.edge===i?'graph-selected-line':''}" d="${d}" marker-end="url(#${marker})"/>${editing?`<path data-graph-hit="${i}" ${attrs} class="graph-hit" d="${d}" data-action="graph-edge" data-index="${i}"><title>${esc(label)}</title></path>`:''}`;
  }).join('');
  if(!editing)lines+=graphRuntimeLines(g,l,boxes)+'<g data-failover-lines pointer-events="none"></g>';
  return `<div class="topology-toolbar" data-key="graph-toolbar"><div class="topology-legend">${editing?`<button class="small ${graphSession.library?'is-active':''}" data-action="graph-add-open">＋ 添加</button><button class="small" data-action="graph-check" data-graph-mutate>整理</button><button class="small" data-action="graph-undo" data-graph-mutate title="撤销 Ctrl/⌘ Z">↶</button><button class="small" data-action="graph-redo" data-graph-mutate title="重做 Ctrl/⌘ Shift Z">↷</button><span class="graph-editor-hint">${graphSession.connect?'连接到左侧 IP 点':'从右侧 IP 点拖线'}</span>`:graphEntryLegend(g)}</div><div class="topology-tools"><button class="small" data-action="canvas-out" aria-label="缩小">−</button><button class="small" data-zoom-value data-action="graph-actual-size">100%</button><button class="small" data-action="canvas-in" aria-label="放大">+</button><button class="small" data-action="canvas-fit">适应</button><button class="small" data-action="canvas-fullscreen">${topologyViews.get(graphSession?.id||l?.id||'new')?.fullscreen?'退出全屏':'全屏'}</button></div></div><div class="graph-workspace" data-key="graph-workspace">${editing&&graphSession.library?graphLibrary():''}<div class="topology graph-viewport" data-key="graph-viewport" tabindex="0" aria-label="拓扑画布"><div class="topology-canvas" data-world-x="${world.x}" data-world-y="${world.y}" style="width:${width}px;height:${height}px"><svg class="topology-cables" width="${width}" height="${height}" viewBox="${world.x} ${world.y} ${width} ${height}"><defs><marker id="graph-arrow" viewBox="0 0 10 10" refX="10" refY="5" markerWidth="5" markerHeight="5" orient="auto"><path d="M 0 0 L 10 5 L 0 10 z" fill="context-stroke"/></marker></defs>${lines}</svg>${graphLayers(g,world)}${cards}</div></div>${editing?graphInspector():`<aside class="graph-failover-popover" data-failover-popover ${graphFailurePreview?.started?'':'hidden'} aria-label="灾备预览">${graphFailureMarkup(g)}</aside>`}</div>`;
}
function graphLayers(g,world) {
  const real=g.vertices.filter(v=>v.kind!=='direct'&&!v.runtime_only&&Number.isInteger(v.layer));
  if(!real.length)return '';
  const top=Math.min(...real.map(v=>v.y))-35;
  return [...new Set(real.map(v=>v.layer))].sort((a,b)=>a-b).map(layer=>{
    const items=real.filter(v=>v.layer===layer),label=layer===0?'入口':items.every(v=>v.kind==='target')?'目标':`第 ${layer} 层`;
    return `<span class="graph-layer-label" style="left:${items[0].x-world.x}px;top:${top-world.y}px">${label}</span>`;
  }).join('');
}
const graphPick=(items,value,empty)=>`<option value="">${empty}</option>`+items.map(x=>`<option value="${esc(x.id)}" ${String(value)===String(x.id)?'selected':''}>${esc(x.name)}</option>`).join('');
function graphEditorControls() {
  const s=graphSession;
  return `<div class="graph-meta" data-key="graph-controls"><input data-graph-field="name" value="${esc(s.name)}" maxlength="64" placeholder="链路名称" aria-label="链路名称"><select data-graph-field="group_id" aria-label="链路分组">${graphPick(canvasView.groups,s.group_id,'默认分组')}</select></div>`;
}
function graphEntryPort(v,g) {
  if(v?.kind!=='entry')return '';
  const s=graphSession,targets=g.vertices.filter(n=>n.kind==='target');
  const target=targets.find(t=>t.id===s.portTarget)||targets[0];
  const value=g.version===2?g.ports?.[`${v.id}/${target?.id}`]:s.port;
  return `${g.version===2&&targets.length>1?`<select data-graph-port-target aria-label="端口对应目标">${targets.map(t=>`<option value="${esc(t.id)}" ${t===target?'selected':''}>${esc(graphName(t))}</option>`).join('')}</select>`:''}<input type="number" data-key="port-${esc(v.id)}/${esc(target?.id||'')}" data-graph-entry-port="${esc(v.id)}" data-target="${esc(target?.id||'')}" min="1" max="65535" placeholder="自动端口" value="${value??''}" aria-label="入口端口" title="入口端口，留空自动分配">`;
}
function graphAddressOptions(v,value,side='in') {
  const n=canvasView.nodes.find(n=>n.id===v.node_id);
  const auto=side==='out'&&graphCurrent().routing_version===2?`<option value="@auto" ${value==='@auto'?'selected':''}>自动出口</option>`:'';
  return '<option value="">选择地址</option>'+auto+graphAddresses(v.id).map(a=>`<option value="${esc(a.id)}" ${a.id===value?'selected':''}>${esc(a.host)}${a.port?':'+a.port:''}${a.primary?' · 主 IP':''}${side==='out'&&nodeAddressProblem(a,n?.network)?' · 出口待设置':''}</option>`).join('');
}
function graphInspector() {
  const s=graphSession,g=s.graph,v=g.vertices.find(v=>v.id===s.selected),e=g.edges[s.edge];
  if(!v&&!e)return '';
  const choices=v?.kind==='target'?canvasView.targets:canvasView.nodes;
  const groups=v?.kind==='target'?canvasView.target_groups||[]:canvasView.node_groups||[];
  const resourceOptions=v?nodeOptions(choices,groups,v.target_id||v.node_id):'';
  const addressFields=e?e.map((id,i)=>{
    const node=g.vertices.find(v=>v.id===id),side=i?'in':'out';
    return node.kind!=='direct'?`<label class="graph-address-field">${i?'接收地址':'发出 IP'}<select data-graph-binding="${side}" data-id="${esc(id)}" aria-label="${i?'接收 IP':'发出 IP'}">${graphAddressOptions(node,graphPortAddress(g,node,side,e),side)}</select></label>`:'';
  }).join(''):'';
  const entryAddress=v?.kind==='entry'?`<label class="graph-address-field">入口接收 IP<select data-graph-listen="${esc(v.id)}" aria-label="入口接收 IP">${graphAddressOptions(v,v.address)}</select></label>`:'';
  const pending=v&&s.connectTarget===v.id?`<label class="graph-address-field">连接到 ${graphLabel(v)}<select data-graph-connect-ip="${esc(v.id)}" aria-label="选择接收 IP">${graphAddressOptions(v,'')}</select></label>`:'';
  const direct=v?.kind==='direct';
  const content=pending||(v?`${direct?`<span>direct</span><label class="graph-address-field">权重<input type="number" data-direct-weight min="-100000" max="100000" step="1" required value="${v.weight??''}" aria-label="direct 权重"></label>`:`<select data-graph-resource aria-label="更换节点">${resourceOptions}</select>`}${entryAddress}${graphEntryPort(v,g)}<button class="small danger" data-action="graph-remove">${direct?'移除 direct':'删除'}</button>`:`<span>${e.map(id=>graphLabel(g.vertices.find(v=>v.id===id))).join(' → ')}</span>${addressFields}${e.every(id=>g.vertices.find(v=>v.id===id)?.kind!=='direct')?'<button class="small" data-action="graph-insert-direct">＋ direct</button>':''}<button class="small danger" data-action="graph-remove-edge">删除连线</button>`);
  return `<div class="graph-inspector" data-key="inspector-${esc(v?.id||e.join('/'))}">${content}<button class="small" data-action="graph-deselect" aria-label="关闭">×</button></div>`;
}
function graphLibrary() {
  const kind=graphSession.addKind||'node',items=kind==='direct'?[{id:'direct',name:'direct',addr:'权重 0'}]:kind==='target'?canvasView.targets:canvasView.nodes;
  const groups=kind==='direct'?[]:kind==='target'?canvasView.target_groups||[]:canvasView.node_groups||[];
  const card=n=>`<button draggable="true" data-action="graph-library-add" data-kind="${kind}" data-id="${n.id}"><strong>${esc(n.name)}</strong>${nodeTags(n.tags,2,false)}<small>${esc(n.addr||n.host||'')}</small></button>`;
  const rows=groups.length?groups.map(group=>{
    const members=items.filter(n=>n.group_id===group.id);
    return members.length?`<section class="graph-library-group" data-group-id="${group.id}"><h4>${esc(group.name)}</h4>${members.map(card).join('')}</section>`:'';
  }).join(''):items.map(card).join('');
  return `<aside class="graph-library"><div class="graph-library-tabs">${[['entry','入口'],['node','节点'],['target','目标'],['direct','direct']].map(([k,n])=>`<button class="${k===kind?'is-active':''}" data-action="graph-library-kind" data-kind="${k}">${n}</button>`).join('')}</div><div class="graph-library-list">${rows}</div></aside>`;
}
function drawGraph() {
  if(graphSession)graphClearFailurePreview();
  const s=graphSession,l=canvasView.link,g=graphCurrent();
  const title=s?.kind==='edit'?(s.id?'编辑链路':'新建链路'):esc(s?.name||l?.name||'选择路径');
  let actions,controls='';
  if(s?.kind==='edit'){
    actions='<button data-action="graph-exit" data-graph-mutate>取消</button><button class="primary" data-action="graph-publish" data-graph-mutate>保存</button>';
    controls=graphEditorControls();
  }else if(s?.kind==='choose'){
    actions='<button data-action="graph-exit" data-graph-mutate>取消选路</button><button class="primary" data-action="graph-autocomplete" data-graph-mutate>自动补全</button>';
    controls=`<div class="graph-selection" data-key="graph-controls">${graphLabel(g.vertices.find(v=>v.id===s.prefix[0]))} · 点击下一跳</div>`;
  }else actions=`${linkState(l)}<button data-action="link-failover" data-graph-mutate aria-pressed="${!!l.failover_enabled}" ${l.state==='deleting'?'disabled':''}>自动灾备${l.failover_enabled?'：开':'：关'}</button><a class="button" href="#/quality/topologies/${l.id}">网络质量</a><button data-action="link-edit" data-graph-mutate ${l.state==='deleting'?'disabled':''}>编辑</button><button class="danger" data-action="link-delete" data-graph-mutate ${l.state==='deleting'?'disabled':''}>删除</button>`;
  render(`<a class="back" href="#/routes">← 链路</a>${pageHead(title,actions)}<section class="panel topology-panel${topologyViews.get(s?.id||l?.id||'new')?.fullscreen?' is-fullscreen':''}"><div class="graph-fullscreen-actions">${actions}</div>${s?.error?`<div class="graph-error" data-key="graph-error" role="alert">${esc(s.error)}${s.conflict&&s.id?'<button class="small" data-action="graph-reload">重新载入</button>':''}</div>`:''}${controls}${graphCanvas(g,l)}${!s?'<div class="graph-route-hint" data-key="graph-hint" data-route-hint></div>':''}${!s?syncFeedback(l):''}</section>${!s?`<div class="graph-summary"><span>${g.vertices.filter(v=>v.kind==='entry'&&!v.runtime_only).length} 个入口 · ${g.vertices.filter(v=>v.kind==='target'&&!v.runtime_only).length} 个目标</span><a href="#/configs">查看配置${l.configurations?' · '+l.configurations.length:''} →</a></div>${graphRouteStatus(g,l)}${detailsBlock('graph-events-'+l.id,'切换记录',l.events.map(e=>`<p>${esc(fmtTime(e.ts))} ${esc(e.message)}</p>`).join('')||'暂无记录')}`:''}`);
  if(s?.kind==='edit'){
    app.querySelectorAll('[data-graph-field]').forEach(el=>{el.value=s[el.dataset.graphField]??'';el.setAttribute('aria-invalid',String(s.errorField===el.dataset.graphField));});
  }
  const key=s?.id||l?.id||'new',view=topologyViews.get(key),world=graphWorld(graphDisplay(g));
  if(view?.graphOrigin){view.x+=(world.x-view.graphOrigin.x)*view.scale;view.y+=(world.y-view.graphOrigin.y)*view.scale;}
  mountTopology(key);topologyBinding.view.graphOrigin={x:world.x,y:world.y};graphButtons();graphPaintConnection();graphFocusEditor();
  graphRestoreFailurePreview();
}


async function graphDetail(id,request) {
  if(graphSession?.id===id)return;
  let d;
  try { d=await api('GET',`topologies/${id}`); }
  catch(e) { if(request!==viewRequest)return;if(e.status!==404)throw e;leaveView();canvasView=null;render('<h1>链路不存在</h1><a href="#/routes">返回链路</a>');return; }
  if(request!==viewRequest)return;
  canvasView=d;drawGraph();
}

function startGraphEditor(l) {
  graphSession={kind:'edit',id:l?.id,revision:l?.revision,name:l?.name||'',group_id:l?.group_id||canvasView.groups[0]?.id,
    port:null,graph:structuredClone(l?.graph||{version:2,routing_version:2,vertices:[],edges:[],bindings:{},ports:{}}),dirty:false,addKind:l?'node':'entry',library:!l};
  graphSession.initial=graphSession.editBase=graphSnapshot(graphSession);graphSession.undo=[];graphSession.redo=[];
  drawGraph();
}

async function graphNew(request) {
  if(graphSession?.kind==='edit'&&!graphSession.id)return;
  const d=await api('GET','topologies');if(request!==viewRequest)return;
  canvasView=d;startGraphEditor(null);
}

function graphValidateFields(s) {
  const invalid=(message,field)=>{const error=new Error(message);error.field=field;throw error;};
  if(s.connect)invalid('请先完成当前连线，或按 Esc 取消','vertex:'+(s.connectTarget||s.connect));
  if(!s.name.trim()||s.name.trim().length>64)invalid('名称需要包含 1 至 64 个字符','name');
  const weight=app.querySelector('[data-direct-weight]');
  if(weight&&!weight.checkValidity())invalid('权重须为 -100000 至 100000 的整数','vertex:'+s.selected);
  const input=app.querySelector('[data-graph-entry-port]');
  if((input&&!input.checkValidity())||(s.port!=null&&(!Number.isInteger(s.port)||s.port<1||s.port>65535)))invalid('端口必须是 1–65535 的整数','port');
}

const graphSnapshot=s=>JSON.stringify({graph:s.graph,name:s.name,group_id:s.group_id,port:s.port});

function graphButtons() {
  const s=graphSession;
  document.querySelectorAll('[data-graph-mutate]').forEach(el=>{
    el.disabled=graphBusy||!!graphDrag||el.dataset.action==='graph-undo'&&!s?.undo?.length||el.dataset.action==='graph-redo'&&!s?.redo?.length||['link-edit','link-delete','link-failover'].includes(el.dataset.action)&&(canvasView.link?.state==='deleting');
  });
  document.querySelectorAll('.graph-library button,.graph-inspector button,.graph-inspector input,.graph-inspector select,[data-graph-field],[data-graph-entry-port],[data-graph-port-target],[data-action="graph-add-open"]').forEach(el=>{
    el.disabled=graphBusy||!!graphDrag;
    if(el.hasAttribute('draggable'))el.draggable=!el.disabled;
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
  s.editBase=graphSnapshot(s);s.selected=null;s.edge=null;graphCancelConnection();graphTouch(false);drawGraph();
}

async function graphPreview(s,prefix,ready=true,arrange=false,advance=false,finish=false) {
  return api('POST','topology-preview',{id:s.id,graph:s.graph,prefix,required:s.required||[],preferred:s.preferred||[],ready,arrange,advance,finish});
}

async function advanceGraph(s,auto=true,prefix=s.prefix) {
  const r=await graphPreview(s,prefix,true,false,auto);
  if(graphSession!==s)return;
  s.prefix=r.prefix;s.options=r.options;s.complete=r.complete;s.plans=r.paths;s.skipped=r.skipped;
  drawGraph();
  if(s.complete&&auto)await confirmGraphChoice(s);
}


async function confirmGraphChoice(s) {
  if(!s?.complete)return;
  const paths={paths:(s.plans||[]).map(p=>p.path)};
  if(!paths.paths.length){toast(s.skipped?.[0]?.reason||'没有可切换的配置',true);return;}
  const r=await api('POST',s.id?`topologies/${s.id}`:'topologies',{graph:s.graph,...paths,name:s.name,group_id:s.group_id,port:s.port,revision:s.revision,switch_only:true});
  const skipped=[...(s.skipped||[]),...(r.skipped||[])].reduce((sum,item)=>sum+(item.count||1),0);
  await graphNavigate(r.id);
  toast(s.editor?'已保存，正在同步':r.changed!=null?`${r.changed?`正在切换 ${r.changed} 项`:'无需切换'}${skipped?` · ${skipped} 项保留原路`:''}${r.skipped?.[0]?.reason?' · '+r.skipped[0].reason:''}`:'正在切换路径',!s.editor&&r.changed===0&&skipped>0);
}


function graphReturnFromPreview(s) {
  graphSession=s.editor||null;
  if(s.editor&&s.returnView)Object.assign(topologyViews.get(s.id||'new'),s.returnView);
  drawGraph();
}

async function startGraphChoice(id,fromEditor=false) {
  const l=canvasView.link,session=graphSession,page=pageVersion;
  if(fromEditor){
    graphValidateFields(session);
    const result=await api('POST',session.id?`topologies/${session.id}`:'topologies',
      {name:session.name,group_id:session.group_id,graph:session.graph,revision:session.revision});
    await graphNavigate(result.id);toast(`已保存 · ${result.configurations} 个配置${result.warnings.length?' · 部分节点未形成完整路径':''}`);return;
  }
  if(l.state==='deleting')return;
  const graph=structuredClone(l.graph),vertex=graph.vertices.find(v=>v.id===id);
  if(!vertex)return toast('已从设计中移除，等待旧路径清理',true);
  const s={kind:'choose',id:l.id,revision:l.revision,graph,name:l.name,group_id:l.group_id,
    prefix:vertex?.kind==='entry'?[id]:[],required:vertex&&vertex.kind!=='entry'?[id]:[],dirty:false};
  const r=await graphPreview(s,s.prefix);
  if(graphSession!==session||pageVersion!==page)return;
  if(!r.paths.length)throw new Error('所选节点没有可达的入口—目标配置');
  s.graph=r.graph;s.plans=r.paths;s.skipped=r.skipped;s.options=r.options;s.complete=r.complete;
  if(vertex?.kind==='entry'){
    graphSession=s;drawGraph();if(s.complete)await confirmGraphChoice(s);
  }else{s.complete=true;await confirmGraphChoice(s);}
}

function graphPosition() {
  const g=graphSession.graph,boxes=graphBoxes(g),curveBoxes=boxes;
  const canvas=app.querySelector('.topology-canvas'),origin={x:Number(canvas.dataset.worldX),y:Number(canvas.dataset.worldY)};
  for(const v of g.vertices) {
    const el=app.querySelector(`[data-graph-node="${v.id}"]`);
    if(el){el.style.left=v.x-origin.x+'px';el.style.top=v.y-origin.y+'px';el.classList.toggle('is-dragging',graphDrag?.node===v);}
  }
  const world=graphWorld(g),svg=canvas.querySelector('.topology-cables');
  Object.assign(svg.style,{left:world.x-origin.x+'px',top:world.y-origin.y+'px'});
  svg.setAttribute('width',world.width);svg.setAttribute('height',world.height);
  svg.setAttribute('viewBox',`${world.x} ${world.y} ${world.width} ${world.height}`);
  graphConnectionRoutes(g,curveBoxes).forEach(({d,i})=>{
    app.querySelectorAll(`[data-graph-line="${i}"],[data-graph-halo="${i}"],[data-graph-hit="${i}"]`).forEach(el=>el.setAttribute('d',d));
  });
}

function graphLinkError(source,target) {
  const g=graphSession.graph;
  const from=g.vertices.find(v=>v.id===source),to=g.vertices.find(v=>v.id===target);
  if(!from||!to)return '连接的节点已不存在';
  if(source===target)return '不能连接组件自身';
  if(from.kind==='target'||to.kind==='entry')return '入口只能向外连接，目标只能接收连接';
  if(from.node_id&&from.node_id===to.node_id)return `两端都是同一台节点「${graphName(from)}」，不能互相转发`;
  if(g.edges.some(([a,b])=>a===source&&b===target))return '这条连线已存在';
  if(from.kind==='direct'&&to.kind==='direct')return 'direct 不能连续连接';
  if(from.kind==='direct'&&g.edges.some(([a])=>a===source)||to.kind==='direct'&&g.edges.some(([,b])=>b===target))return 'direct 只能连入和连出各一条线';
  const left=from.kind==='direct'?g.vertices.find(v=>v.id===g.edges.find(([,b])=>b===source)?.[0]):from;
  const right=to.kind==='direct'?g.vertices.find(v=>v.id===g.edges.find(([a])=>a===target)?.[1]):to;
  if(left?.node_id&&left.node_id===right?.node_id)return 'direct 两端不能是同一台节点';
  const seen=new Set(),pending=[target];
  while(pending.length){const id=pending.pop();if(id===source)return '这条连线会形成循环';if(seen.has(id))continue;seen.add(id);g.edges.forEach(([a,b])=>{if(a===id)pending.push(b);});}
  return '';
}


function graphChooseAddress(id,side,address,onlyEdge=null) {
  const g=graphSession.graph,v=g.vertices.find(v=>v.id===id);if(!v||g.version!==2||!address)return;
  v[side+'_address']=address;
  if(v.kind==='entry'&&side==='in'){v.address=address;return;}
  g.bindings||={};
  // A node preference must never rewrite the addresses of its other edges.
  for(const edge of onlyEdge?[onlyEdge]:[]){
    if(edge[side==='in'?1:0]!==id)continue;
    const binding=g.bindings[edge.join('/')]||={from:{},to:{}};
    (binding[side==='in'?'to':'from']||={})[id]=address;
  }
}
function graphCancelConnection() {
  if(!graphSession)return;
  for(const key of ['connect','connectVertex','connectAddress','connectTarget'])graphSession[key]=null;
}
function graphStartConnection(id,address) {
  const s=graphSession,v=s.graph.vertices.find(v=>v.id===id);
  graphCancelConnection();s.edge=null;s.selected=null;s.error='';s.errorField='';
  if(!v||v.kind==='target')return false;
  if(v.node_id&&!(s.graph.routing_version===2&&address==='@auto')&&!graphAddresses(id).some(a=>a.id===address)){
    s.error=`请先为「${graphName(v)}」配置发出 IP`;s.errorField='vertex:'+id;drawGraph();return false;
  }
  s.connect=s.connectVertex=id;s.connectAddress=address;
  drawGraph();return true;
}
function graphConnect(id,side,address) {
  const s=graphSession,g=s.graph,owner=id;
  if(side==='out'){
    graphStartConnection(id,address);return;
  }
  if(!s.connect){s.selected=id;s.edge=null;drawGraph();return;}
  const error=graphLinkError(s.connect,owner);if(error){s.error=error;drawGraph();return;}
  const target=g.vertices.find(v=>v.id===id),addresses=graphAddresses(id,g);
  // A whole-card drop is unambiguous only for a single-address node. Keep the
  // wire pending until the user explicitly chooses an IP on a multi-IP node.
  if(target.kind!=='direct'&&!addresses.some(a=>a.id===address)){
    if(addresses.length===1)address=addresses[0].id;
    else {
      s.connectTarget=id;s.selected=id;
      s.error=addresses.length?`请选择「${graphName(target)}」的接收 IP`:`请先为「${graphName(target)}」配置接收 IP`;
      s.errorField='vertex:'+id;drawGraph();return;
    }
  }
  const edge=[s.connect,owner];
  if(g.version===2){
    g.bindings||={};const binding={from:{},to:{}};
    for(const [index,key,direction] of [[0,'from','out'],[1,'to','in']])for(const v of g.vertices.filter(v=>v.id===edge[index]&&v.kind!=='direct')){
      const explicit=index===0&&v.id===s.connectVertex?s.connectAddress:index===1&&v.id===id?address:null;
      const chosen=explicit;
      if(chosen){binding[key][v.id]=chosen;v[direction+'_address']=chosen;}
    }
    g.bindings[edge.join('/')]=binding;
  }
  g.edges.push(edge);
  graphCancelConnection();s.edge=g.edges.length-1;s.selected=null;graphTouch();drawGraph();
}
function graphAddResource(kind,id,point=null) {
  if(graphBusy||graphSession?.kind!=='edit')return;
  const s=graphSession,g=s.graph;
  if(!['entry','node','target','direct'].includes(kind)||!id)return;
  if(['entry','target'].includes(kind)&&g.vertices.some(v=>v.kind===kind))g.version=2;
  const v={id:graphID(),kind,...(kind==='direct'?{weight:0}:{[kind==='target'?'target_id':'node_id']:Number(id)}),...(point||graphAddPosition(kind==='direct'?110:220,kind==='direct'?56:110))};
  g.vertices.push(v);
  if(v.node_id)v.out_address='@auto';
  const addresses=graphAddresses(v.id,g);if(addresses.length===1){v.in_address=addresses[0].id;if(kind==='entry')v.address=addresses[0].id;}
  graphCancelConnection();
  if(kind==='entry')s.addKind='node';
  s.selected=v.id;s.edge=null;graphTouch();drawGraph();graphReveal(v);
}

function graphInsertDirect() {
  if(graphBusy||graphSession?.kind!=='edit')return;
  const s=graphSession,g=s.graph,e=g.edges[s.edge];if(!e)return;
  const [a,b]=e.map(id=>g.vertices.find(v=>v.id===id));if(!a||!b||a.kind==='direct'||b.kind==='direct')return;
  const binding=g.bindings?.[e.join('/')]||{from:{},to:{}};
  const v={id:graphID(),kind:'direct',weight:0,x:Math.round((a.x+graphWidth(a)+b.x-110)/2),y:Math.round((a.y+graphHeight(a,g)/2+b.y+graphHeight(b,g)/2)/2-28)};
  g.vertices.push(v);g.edges.splice(s.edge,1,[a.id,v.id],[v.id,b.id]);g.bindings||={};
  delete g.bindings[e.join('/')];
  g.bindings[`${a.id}/${v.id}`]={from:binding.from||{},to:{}};
  g.bindings[`${v.id}/${b.id}`]={from:{},to:binding.to||{}};
  s.selected=v.id;s.edge=null;graphTouch();drawGraph();
}
function graphRemoveSelected() {
  if(graphBusy)return;
  const s=graphSession,g=s.graph,id=s.selected,v=g.vertices.find(v=>v.id===id);if(!v)return;
  const before=g.edges.find(([,b])=>b===id),after=g.edges.find(([a])=>a===id);
  if(v.kind==='direct'&&before&&after){
    if(g.edges.some(([a,b])=>a===before[0]&&b===after[1])){s.error='两端已有另一条直连，请先删除其中一条连线';drawGraph();return;}
    const joined=[before[0],after[1]];g.edges.push(joined);g.bindings||={};
    g.bindings[joined.join('/')]={from:g.bindings[before.join('/')]?.from||{},to:g.bindings[after.join('/')]?.to||{}};
  }
  g.vertices=g.vertices.filter(v=>v.id!==id);
  g.edges=g.edges.filter(([a,b])=>a!==id&&b!==id);
  for(const key of Object.keys(g.bindings||{}))if(key.split('/').includes(id))delete g.bindings[key];
  s.selected=null;graphCancelConnection();graphTouch();drawGraph();
}

function graphReveal(v) {
  const b=topologyBinding,canvas=app.querySelector('.topology-canvas'),x=(v.x-Number(canvas.dataset.worldX))*b.view.scale+b.view.x,y=(v.y-Number(canvas.dataset.worldY))*b.view.scale+b.view.y;
  const width=graphWidth(v)*b.view.scale,height=graphHeight(v,graphCurrent())*b.view.scale;
  if(x<24)b.view.x+=24-x;else if(x+width>b.viewport.clientWidth-24)b.view.x+=b.viewport.clientWidth-24-x-width;
  if(y<24)b.view.y+=24-y;else if(y+height>b.viewport.clientHeight-24)b.view.y+=b.viewport.clientHeight-24-y-height;
  b.view.auto=false;b.apply();
}


let graphWire=null;
function graphPaintConnection(point) {
  const s=graphSession,svg=app.querySelector('.topology-cables');
  if(s?.kind!=='edit'||!svg)return;
  app.querySelectorAll('.graph-port').forEach(el=>{
    const error=s.connect?graphLinkError(s.connect,el.dataset.id):'';
    el.classList.toggle('graph-link-source',el.dataset.id===s.connect&&el.dataset.side==='out'&&el.dataset.address===s.connectAddress);
    el.classList.toggle('graph-link-target',!!s.connect&&el.dataset.side==='in'&&!error);
    if(s.connect&&el.dataset.side==='in')el.title=error||'连接到此 IP';
  });
  if(!s.connect)return;
  const boxes=graphBoxes(s.graph),source=boxes.get(s.connectVertex||s.connect);if(!source)return;
  let path=svg.querySelector('.graph-wire');
  if(!path){path=document.createElementNS('http://www.w3.org/2000/svg','path');path.setAttribute('class','graph-wire');svg.append(path);}
  const canvas=app.querySelector('.topology-canvas'),rect=canvas.getBoundingClientRect(),scale=topologyBinding.view.scale;
  const x=point?(point.x-rect.left)/scale+Number(canvas.dataset.worldX):source.x+source.width+70;
  const y=point?(point.y-rect.top)/scale+Number(canvas.dataset.worldY):source.y+source.height/2;
  const [sx,sy]=graphPortPoint(s.graph,s.connectVertex||s.connect,'out',s.connectAddress,boxes),mid=(sx+x)/2;
  path.setAttribute('d',graphRoundedPath([[sx,sy],[mid,sy],[mid,y],[x,y]]));
}

document.addEventListener('pointerdown',e=>{
  const port=e.target.closest('.graph-port.out');
  if(!port||graphSession?.kind!=='edit'||graphBusy||graphDrag||e.button!==0)return;
  e.preventDefault();e.stopPropagation();
  if(!graphStartConnection(port.dataset.id,port.dataset.address))return;
  graphWire={pointer:e.pointerId,x:e.clientX,y:e.clientY};
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
  if(e.type==='pointercancel'){graphCancelConnection();drawGraph();}
  else if(wire.moved) {
    const hit=document.elementFromPoint(e.clientX,e.clientY),card=hit?.closest('[data-graph-node]');
    if(card){
      const row=hit.closest('[data-graph-address]');
      graphConnect(card.dataset.graphNode,'in',hit.closest('.graph-port')?.dataset.address||row?.dataset.graphAddress);
    }else {
      graphCancelConnection();graphSession.error='未连接到节点，请拖到目标节点或其 IP 行';drawGraph();
    }
  }
  else drawGraph();
  setTimeout(()=>graphDragConsumed=false,0);
}
document.addEventListener('pointerup',graphEndWire);document.addEventListener('pointercancel',graphEndWire);

let graphDrag=null;
document.addEventListener('pointerdown',e=>{
  const el=e.target.closest('[data-graph-drag]');
  if(!el||graphDrag||graphSession?.kind!=='edit'||graphBusy||e.button!==0||graphSession.connect)return;
  e.preventDefault();e.stopPropagation();
  const items=graphSession.graph.vertices.filter(v=>v.id===el.dataset.id);
  if(!items.length)return;
  graphSession.selected=el.dataset.id;graphSession.edge=null;
  graphDrag={x:e.clientX,y:e.clientY,scale:topologyBinding.view.scale,points:items.map(v=>({v,x:v.x,y:v.y})),pointer:e.pointerId,
    node:items[0],element:el,dirty:graphSession.dirty,viewX:topologyBinding.view.x,viewY:topologyBinding.view.y};
  try{el.setPointerCapture(e.pointerId);}catch(_){} // Synthetic test events have no active pointer.
},true);
function graphMoveDrag(x,y) {
  const drag=graphDrag;if(!drag)return;
  drag.clientX=x;drag.clientY=y;
  const view=topologyBinding.view;
  const dx=(x-drag.x-(view.x-drag.viewX))/drag.scale,dy=(y-drag.y-(view.y-drag.viewY))/drag.scale;
  if(Math.abs(dx)+Math.abs(dy)<4&&!drag.moved)return;
  drag.moved=true;graphDragConsumed=true;graphTouch(false);view.auto=false;
  for(const p of drag.points){p.v.x=Math.round(p.x+dx);p.v.y=Math.round(p.y+dy);}
  if(!drag.renderFrame)drag.renderFrame=requestAnimationFrame(()=>{drag.renderFrame=null;if(graphDrag===drag)graphPosition();});
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
  const drag=graphDrag,cancelled=e.type==='pointercancel';
  if(drag.frame)cancelAnimationFrame(drag.frame);
  if(drag.renderFrame)cancelAnimationFrame(drag.renderFrame);
  if(drag.moved&&cancelled) {
    for(const point of drag.points)Object.assign(point.v,{x:point.x,y:point.y});
    graphSession.dirty=drag.dirty;
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
  if(e.target.hasAttribute('data-direct-weight')){
    const v=graphSession.graph.vertices.find(v=>v.id===graphSession.selected);
    if(v?.kind==='direct'){v.weight=e.target.value===''?null:Number(e.target.value);graphTouch(false);}
  }
  const field=e.target.dataset.graphField;
  if(field){graphSession[field]=field==='name'?e.target.value:e.target.value===''?null:Number(e.target.value);graphTouch(false);}
  if(e.target.hasAttribute('data-graph-entry-port')) {
    const value=e.target.value===''?null:Number(e.target.value),g=graphSession.graph;
    if(g.version===2){const key=`${e.target.dataset.graphEntryPort}/${e.target.dataset.target}`;g.ports||={};if(value==null)delete g.ports[key];else g.ports[key]=value;}
    else graphSession.port=value;
    graphTouch(false);
  }
});
document.addEventListener('change',e=>{
  if(graphSession?.kind!=='edit'||graphBusy)return;
  if(e.target.hasAttribute('data-graph-connect-ip')){
    if(e.target.value)graphConnect(e.target.dataset.graphConnectIp,'in',e.target.value);
    return;
  }
  if(e.target.hasAttribute('data-graph-listen')){
    if(e.target.value)graphChooseAddress(e.target.dataset.graphListen,'in',e.target.value);
    graphTouch();drawGraph();return;
  }
  if(e.target.hasAttribute('data-graph-binding')){
    const edge=graphSession.graph.edges[graphSession.edge];
    if(edge&&e.target.value)graphChooseAddress(e.target.dataset.id,e.target.dataset.graphBinding,e.target.value,edge);
    graphTouch();drawGraph();return;
  }
  if(e.target.hasAttribute('data-graph-field')||e.target.hasAttribute('data-graph-entry-port')||e.target.hasAttribute('data-direct-weight'))graphTouch();
  if(e.target.hasAttribute('data-graph-port-target')){graphSession.portTarget=e.target.value;drawGraph();}
  const prop=e.target.dataset.graphProperty,v=graphSession.graph.vertices.find(v=>v.id===graphSession.selected);
  if(e.target.hasAttribute('data-graph-resource')&&v) {
    const id=Number(e.target.value);
    if((v.target_id||v.node_id)===id)return;
    v[v.kind==='target'?'target_id':'node_id']=id;
    if(v.node_id||v.target_id){
      delete v.address;delete v.in_address;delete v.out_address;
      for(const binding of Object.values(graphSession.graph.bindings||{}))for(const side of ['from','to'])delete binding[side]?.[v.id];
      const addresses=graphAddresses(v.id,graphSession.graph);
      for(const side of v.kind==='target'?['in']:['in','out']){
        const address=v.kind==='target'?'main':side==='out'?'@auto':addresses.length===1?addresses[0].id:null;
        if(!address)continue;
        graphChooseAddress(v.id,side,address);
        for(const edge of graphSession.graph.edges)graphChooseAddress(v.id,side,address,edge);
      }
    }
    graphTouch();drawGraph();
  }
});

document.addEventListener('keydown',e=>{
  if(e.key==='Escape'&&graphSession?.kind==='choose'&&!graphBusy){e.preventDefault();e.stopImmediatePropagation();graphReturnFromPreview(graphSession);return;}
  if(graphSession?.kind!=='edit'||!$('#modal').hidden)return;
  if(e.key==='Escape'&&graphDrag){e.preventDefault();e.stopImmediatePropagation();endGraphDrag({pointerId:graphDrag.pointer,type:'pointercancel'});return;}
  if(e.key==='Escape'&&graphWire){e.preventDefault();e.stopImmediatePropagation();graphEndWire({pointerId:graphWire.pointer,type:'pointercancel'});return;}
  if(e.key==='Escape'&&graphSession.connect){e.preventDefault();e.stopImmediatePropagation();graphCancelConnection();graphSession.error='';graphSession.errorField='';drawGraph();return;}
  if(e.target.closest('input,select,textarea,[contenteditable="true"]')||graphBusy)return;
  if((e.ctrlKey||e.metaKey)&&['z','y'].includes(e.key.toLowerCase())){e.preventDefault();graphUndo(e.shiftKey||e.key.toLowerCase()==='y');return;}
  if(e.key==='Escape'&&(graphSession.connect||graphSession.selected||graphSession.edge!=null)) {e.preventDefault();e.stopImmediatePropagation();graphCancelConnection();graphSession.selected=null;graphSession.edge=null;graphSession.error='';graphSession.errorField='';drawGraph();}
  if(['Delete','Backspace'].includes(e.key)&&!graphDrag) {
    e.preventDefault();const s=graphSession;
    if(s.edge!=null)ACTIONS['graph-remove-edge']();
    else if(s.selected)ACTIONS['graph-remove']();
  }
},true);
document.addEventListener('click',e=>{
  if(graphSession?.kind!=='edit'||graphBusy||graphDragConsumed||!e.target.closest('.graph-viewport')||e.target.closest('[data-action]'))return;
  graphCancelConnection();graphSession.selected=null;graphSession.edge=null;drawGraph();
});
window.addEventListener('blur',()=>{
  if(graphDrag)endGraphDrag({pointerId:graphDrag.pointer,type:'pointercancel'});
  if(graphWire)graphEndWire({pointerId:graphWire.pointer,type:'pointercancel'});
});

Object.assign(ACTIONS,{
  'link-new':()=>{location.hash='#/routes/new';},
  'link-edit':()=>{if(!graphBusy&&canvasView.link.state!=='deleting')startGraphEditor(canvasView.link);},
  'graph-actual-size':()=>topologyBinding?.zoom(1/topologyBinding.view.scale),
  'graph-undo':()=>graphUndo(),
  'graph-redo':()=>graphUndo(true),
  'graph-autocomplete':()=>graphWork(async()=>{
    const s=graphSession,r=await graphPreview(s,s.prefix,true,false,false,true);
    s.plans=r.paths;s.skipped=r.skipped;s.complete=true;await confirmGraphChoice(s);
  }),

  'graph-add-open':()=>{if(graphBusy)return;graphSession.library=!graphSession.library;drawGraph();},
  'graph-library-kind':el=>{if(graphBusy)return;graphSession.addKind=el.dataset.kind;drawGraph();},
  'graph-library-add':el=>graphAddResource(el.dataset.kind,el.dataset.id),
  'graph-deselect':()=>{if(graphBusy)return;graphSession.selected=null;graphSession.edge=null;graphCancelConnection();drawGraph();},
  'graph-listen':el=>{if(graphBusy)return;if(graphSession.connect)return graphConnect(el.dataset.id,'in',el.dataset.address);graphChooseAddress(el.dataset.id,'in',el.dataset.address);graphTouch();drawGraph();},
  'graph-receive':el=>{if(graphBusy||graphDragConsumed)return;const v=graphSession.graph.vertices.find(v=>v.id===el.dataset.id);if(v?.kind==='entry'&&!graphSession.connect)return ACTIONS['graph-listen'](el);graphConnect(el.dataset.id,'in',el.dataset.address);},


  'graph-port':el=>{if(graphDragConsumed||graphSession?.kind!=='edit'||graphBusy)return;graphConnect(el.dataset.id,el.dataset.side,el.dataset.address);},
  'graph-select':el=>{
    if(graphDragConsumed||graphBusy)return;
    const s=graphSession,id=el.dataset.id;
    if(!s){
      const v=canvasView.link.graph.vertices.find(v=>v.id===id);
      if(v?.kind==='target')return;
      return graphWork(()=>startGraphChoice(id));
    }
    if(s.kind==='choose') {
      if(s.graph.vertices.find(v=>v.id===id)?.kind==='target')return;
      if(s.options.includes(id)){s.error='';return graphWork(()=>advanceGraph(s,true,[...s.prefix,id]));}
      if(s.prefix.includes(id)&&!s.complete)return ACTIONS['graph-rewind'](el);
      if(s.graph.vertices.find(v=>v.id===id)?.kind==='entry')return graphWork(()=>startGraphChoice(id));
      return;
    }
    if(s.connect)return graphConnect(id,'in');
    s.selected=id;s.edge=null;drawGraph();
  },
  'graph-edge':el=>{if(graphSession?.kind!=='edit'||graphBusy)return;graphCancelConnection();graphSession.edge=Number(el.dataset.index);graphSession.selected=null;drawGraph();},
  'graph-insert-direct':graphInsertDirect,
  'graph-remove':graphRemoveSelected,
  'graph-remove-edge':()=>{if(graphBusy)return;const s=graphSession,e=s.graph.edges[s.edge];if(!e)return;delete s.graph.bindings?.[e.join('/')];s.graph.edges.splice(s.edge,1);s.edge=null;graphTouch();drawGraph();},
  'graph-check':()=>graphWork(async()=>{const s=graphSession,page=pageVersion,r=await api('POST','topology-preview',{graph:s.graph,layout_only:true});if(graphSession!==s||pageVersion!==page)return;s.graph=r.graph;graphTouch();drawGraph();topologyBinding?.fit();}),
  'graph-publish':()=>graphWork(()=>startGraphChoice(null,true)),
  'graph-rewind':el=>graphWork(async()=>{const s=graphSession,index=s.prefix.indexOf(el.dataset.id);if(index<0)return;s.prefix=s.prefix.slice(0,index+1);s.complete=false;s.error='';await advanceGraph(s,false);}),
  'graph-reload':()=>graphWork(async()=>{
    const s=graphSession,editing=s.kind==='edit'||!!s.editor;
    if((s.dirty||s.editor?.dirty)&&!await confirmBox('重新载入？','将放弃当前未保存的编辑，载入服务端最新状态。','载入'))return;
    const data=await api('GET',`topologies/${s.id}`);canvasView=data;
    if(editing&&data.link.state!=='deleting')startGraphEditor(data.link);
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

// Resources can be dropped directly at their final position; tapping also works.
document.addEventListener('dragstart',e=>{
  const tile=e.target.closest('[draggable][data-kind]');if(!tile)return;
  if(graphBusy){e.preventDefault();return;}
  e.dataTransfer.setData('application/x-ictl-component',JSON.stringify({kind:tile.dataset.kind,id:tile.dataset.id}));e.dataTransfer.effectAllowed='copy';
});
document.addEventListener('dragover',e=>{if(graphSession?.kind==='edit'&&e.target.closest('.graph-viewport')&&Array.from(e.dataTransfer.types).includes('application/x-ictl-component'))e.preventDefault();});
document.addEventListener('drop',e=>{
  if(graphBusy||graphSession?.kind!=='edit'||!e.target.closest('.graph-viewport'))return;
  const value=e.dataTransfer.getData('application/x-ictl-component');if(!value)return;e.preventDefault();
  const data=JSON.parse(value),canvas=app.querySelector('.topology-canvas'),r=canvas.getBoundingClientRect(),scale=topologyBinding.view.scale;
  graphAddResource(data.kind,data.id,{x:Math.round((e.clientX-r.left)/scale+Number(canvas.dataset.worldX)-110),y:Math.round((e.clientY-r.top)/scale+Number(canvas.dataset.worldY)-40)});
});
