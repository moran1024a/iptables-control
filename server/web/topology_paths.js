// Runtime routes share the same explicit node connections as the editor.
let graphRouteCache=null,graphHintRequest=0,graphHintTimer=null;

function graphRoutes(link) {
  if(link?.selection_mode==='independent')return (link.configurations||[]).flatMap(c=>[
    {id:c.id,entry:c.entry,target:c.target,path:c.active_path||[],pending:false,state:c.state},
    {id:c.id,entry:c.entry,target:c.target,path:c.pending_path||[],pending:true,state:c.state}
  ]).filter(r=>r.path.length>1);
  return [link?.active_path||[],link?.job?.plan?.graph_path||[]].flatMap((path,i)=>
    path.length>1?[{id:link.id,entry:path[0],target:path.at(-1),path,pending:!!i,state:link.state}]:[]);
}

function graphEntryColors(g) {
  const colors=new Map(),hues=[];
  for(const v of g.vertices.filter(v=>v.kind==='entry').sort((a,b)=>a.id.localeCompare(b.id))){
    let hash=0;for(const ch of v.id)hash=(hash*31+ch.charCodeAt(0))>>>0;
    let hue=hash%360;
    for(let i=0;i<12&&hues.some(h=>Math.min(Math.abs(h-hue),360-Math.abs(h-hue))<28);i++)hue=(hue+137.5)%360;
    hues.push(hue);colors.set(v.id,`hsl(${hue.toFixed(1)} 66% 47%)`);
  }
  return colors;
}

function graphEntryLegend(g) {
  const colors=graphEntryColors(g);
  return g.vertices.filter(v=>v.kind==='entry').map(v=>`<button class="graph-entry-chip" data-action="graph-select" data-id="${esc(v.id)}" data-entry="${esc(v.id)}" style="--route-color:${colors.get(v.id)}" title="从 ${esc(graphName(v))} 选择线路"><i></i>${esc(graphName(v))}</button>`).join('');
}

function graphEntryMarks(g,link,id) {
  const entries=new Set(graphRoutes(link).filter(r=>!r.pending&&r.path.includes(id)).map(r=>r.entry)),colors=graphEntryColors(g);
  return [...entries].filter(entry=>colors.has(entry)).map(entry=>`<i class="graph-entry-dot" style="background:${colors.get(entry)}" title="${esc(graphName(g.vertices.find(v=>v.id===entry)))}"></i>`).join('');
}

function graphLanePenalty(a,b,used) {
  const horizontal=a[1]===b[1];let cost=0;
  for(const [c,d] of used){
    const same=horizontal===(c[1]===d[1]),axis=horizontal?0:1,other=1-axis;
    if(same){
      const overlap=Math.min(Math.max(a[axis],b[axis]),Math.max(c[axis],d[axis]))-Math.max(Math.min(a[axis],b[axis]),Math.min(c[axis],d[axis]));
      if(overlap>2&&Math.abs(a[other]-c[other])<6)cost+=1000+overlap*15;
    }else if(c[axis]>Math.min(a[axis],b[axis])&&c[axis]<Math.max(a[axis],b[axis])&&a[other]>Math.min(c[other],d[other])&&a[other]<Math.max(c[other],d[other]))cost+=35;
  }
  return cost;
}

function graphRuntimeLines(g,link,boxes) {
  const routes=graphRoutes(link),colors=graphEntryColors(g),entries=[...colors.keys()];
  if(graphSession?.kind==='choose'&&graphSession.prefix.length>1)routes.push({id:'preview',entry:graphSession.prefix[0],path:graphSession.prefix,pending:true});
  const spans=new Map();
  const live=new Set(routes.filter(r=>!r.pending).flatMap(r=>r.path.slice(1).map((b,i)=>JSON.stringify([r.entry,r.path[i],b]))));
  for(const r of routes)for(let i=1;i<r.path.length;i++){
    const a=r.path[i-1],b=r.path[i],key=JSON.stringify([r.entry,a,b,r.pending]);
    if(r.pending&&live.has(JSON.stringify([r.entry,a,b])))continue;
    if(boxes.has(a)&&boxes.has(b))spans.set(key,{a,b,entry:r.entry,pending:r.pending});
  }
  const list=[...spans.values()].sort((a,b)=>entries.indexOf(a.entry)-entries.indexOf(b.entry)||Number(a.pending)-Number(b.pending)||a.a.localeCompare(b.a)||a.b.localeCompare(b.b));
  // Port rows use the live address inventory, not only the published snapshot.
  const ports=g.vertices.map(v=>graphAddresses(v.id,g).map(a=>a.id));
  const key=JSON.stringify([g.vertices,g.bindings,list,ports]);
  if(graphRouteCache?.key!==key){
    const used=[],ports=new Map();
    // Allocate both ends by the actual IP port. Distinct source edges can still
    // converge on the same receiving port and must not reuse its arrow position.
    for(const span of list){
      const edge=[span.a,span.b];
      for(const [side,id] of [['out',span.a],['in',span.b]]){
        const vertex=g.vertices.find(v=>v.id===id),address=graphPortAddress(g,vertex,side,edge);
        const key=JSON.stringify([id,side,address]);
        if(!ports.has(key))ports.set(key,[]);
        ports.get(key).push({span,side,point:graphPortPoint(g,id,side,address,boxes)});
      }
    }
    for(const connections of ports.values()){
      // Outgoing branches of one entry share their origin; incoming arrows each
      // get a slot, including the old and pending paths during a switch.
      const slot=c=>c.side==='out'?JSON.stringify([c.span.entry,c.span.pending]):c.span;
      const slots=[...new Set(connections.map(slot))],spacing=Math.min(8,16/Math.max(1,slots.length-1));
      for(const c of connections){
        c.point[1]+=(slots.indexOf(slot(c))-(slots.length-1)/2)*spacing;
        c.span[c.side]={point:c.point,spacing,lane:slots.indexOf(slot(c))};
      }
    }
    for(const span of list){
      const edge=[span.a,span.b];
      const start=span.out.point,end=span.in.point,lane=span.in.lane;
      const source=boxes.get(edge[0]),target=boxes.get(edge[1]);
      const obstacles=[...boxes.values()].filter(box=>box.id!==span.a&&box.id!==span.b);
      const all=[source,target,...obstacles.filter(box=>box.id!==source.id&&box.id!==target.id)];
      const points=graphRoutePoints(start,end,source,target,all,lane,used.filter(s=>s.entry!==span.entry||s.pending!==span.pending).map(s=>s.points));
      span.d=graphRoundedPath(points);
      const spacing=Math.min(span.out.spacing,span.in.spacing);
      span.stroke=Math.min(1.7,spacing*.45);
      span.halo=Math.min(6,spacing*.75);
      span.arrowHeight=Math.min(6,span.in.spacing*.65);
      // Tiny endpoint joins may coincide; long collinear runs receive a strong penalty.
      for(let i=1;i<points.length;i++)if(Math.hypot(points[i][0]-points[i-1][0],points[i][1]-points[i-1][1])>4)used.push({entry:span.entry,pending:span.pending,points:[points[i-1],points[i]]});
    }
    graphRouteCache={key,list};
  }
  const focus=graphSession?.kind==='choose'?graphSession.prefix[0]:null;
  return graphRouteCache.list.map((r,i)=>{
    const muted=focus&&focus!==r.entry?' graph-route-muted':'',arrow=`graph-route-arrow-${i}`;
    const attrs=`data-graph-from="${esc(r.a)}" data-graph-to="${esc(r.b)}" data-route-entry="${esc(r.entry)}" style="--route-color:${colors.get(r.entry)};--route-stroke:${r.stroke};--route-halo:${r.halo}" d="${r.d}"`;
    return `<g class="graph-route${muted}" data-runtime-route="${i}"><defs><marker id="${arrow}" viewBox="0 0 7 6" refX="7" refY="3" markerUnits="userSpaceOnUse" markerWidth="7" markerHeight="${r.arrowHeight}" preserveAspectRatio="none" orient="auto"><path d="M 0 0 L 7 3 L 0 6 Z" fill="context-stroke"/></marker></defs><path class="graph-route-halo" ${attrs}/><path class="graph-line graph-route-line ${r.pending?'graph-next':'graph-live'}" ${attrs} marker-end="url(#${arrow})"/>${!r.pending?`<path class="topology-flow graph-route-flow" ${attrs}/>`:''}</g>`;
  }).join('');
}

function graphRouteStatus(g,link) {
  if(link?.selection_mode!=='independent')return '';
  const colors=graphEntryColors(g),name=id=>graphName(g.vertices.find(v=>v.id===id));
  const rows=link.configurations.map(c=>`<div class="graph-route-status" data-config-route="${c.id}"><span><i class="graph-entry-dot" style="background:${colors.get(c.entry)}"></i>${esc(name(c.entry))} → ${esc(name(c.target))}</span><span class="graph-route-description" title="${esc(c.active_path.map(name).join(' → '))}">${esc(c.active_path.slice(1,-1).map(name).join(' → ')||'direct')}</span>${linkState(c)}${c.error?`<small>${esc(c.error)}</small>`:''}</div>`).join('');
  return detailsBlock('graph-paths-'+link.id,'路径状态',rows);
}

function graphQuickHint(id) {
  clearTimeout(graphHintTimer);const token=++graphHintRequest;
  if(rendering||graphSession||poolView.link?.selection_mode!=='independent'||poolView.link.job&&poolView.link.job.kind!=='switch')return;
  const hint=app.querySelector('[data-route-hint]');if(hint)hint.textContent='';
  if(!id)return;
  const link=poolView.link,page=pageVersion;
  const vertex=poolView.link.active_graph?.vertices.find(v=>v.id===id);
  if(!vertex||!['node','direct'].includes(vertex.kind))return;
  graphHintTimer=setTimeout(async()=>{
    try{
      const result=await api('POST','topology-preview',{id:poolView.link.id,graph:poolView.link.active_graph,required:[id]});
      if(token!==graphHintRequest||graphSession||page!==pageVersion||poolView.link!==link||!hint?.isConnected)return;
      const changed=result.paths.filter(p=>!p.unchanged),entries=[...new Set(changed.map(p=>p.entry))],targets=[...new Set(changed.map(p=>p.target))],g=poolView.link.active_graph;
      const names=ids=>ids.map(id=>graphName(g.vertices.find(v=>v.id===id))).join('、');
      if(hint)hint.textContent=changed.length?`可切换 ${changed.length} 个配置：${names(entries)} → ${names(targets)}`:'没有需要切换的可用配置';
    }catch(_){/* Hover is optional; clicking still performs authoritative validation. */}
  },180);
}
