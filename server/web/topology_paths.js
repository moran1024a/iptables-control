// Runtime routes share the same explicit node connections as the editor.
let graphRouteCache=null,graphConnectionCache=null;
let graphFailurePreview=null,graphHintTimer=null,graphHintClose=null;

function graphRoutes(link) {
  return (link?.configurations||[]).flatMap(c=>[
    {id:c.id,entry:c.entry,target:c.target,path:c.active_path||[],graph:c.active_graph,pending:false,state:c.state},
    {id:c.id,entry:c.entry,target:c.target,path:c.pending_path||[],graph:c.pending_graph,pending:true,state:c.state}
  ]).filter(r=>r.path.length>1);
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
  return g.vertices.filter(v=>v.kind==='entry'&&!v.runtime_only).map(v=>`<button class="graph-entry-chip" data-action="graph-select" data-id="${esc(v.id)}" data-entry="${esc(v.id)}" style="--route-color:${colors.get(v.id)}" title="从 ${esc(graphName(v))} 选择线路"><i></i>${graphLabel(v)}</button>`).join('');
}

function graphEntryMarks(g,link,id) {
  const entries=new Set(graphRoutes(link).filter(r=>!r.pending&&r.path.includes(id)).map(r=>r.entry)),colors=graphEntryColors(g);
  return [...entries].filter(entry=>colors.has(entry)).map(entry=>`<i class="graph-entry-dot" style="background:${colors.get(entry)}" title="${esc(graphName(g.vertices.find(v=>v.id===entry)))}"></i>`).join('');
}

function graphLanePenalty(a,b,used,clearance=6) {
  const horizontal=a[1]===b[1];let cost=0;
  for(const [c,d] of used){
    const same=horizontal===(c[1]===d[1]),axis=horizontal?0:1,other=1-axis;
    if(same){
      const overlap=Math.min(Math.max(a[axis],b[axis]),Math.max(c[axis],d[axis]))-Math.max(Math.min(a[axis],b[axis]),Math.min(c[axis],d[axis]));
      if(overlap>2&&Math.abs(a[other]-c[other])<clearance)cost+=1000+overlap*15;
    }else if(c[axis]>Math.min(a[axis],b[axis])&&c[axis]<Math.max(a[axis],b[axis])&&a[other]>Math.min(c[other],d[other])&&a[other]<Math.max(c[other],d[other]))cost+=35;
  }
  return cost;
}

function graphConnectionRoutes(g,boxes,editing=true) {
  const edges=graphConnections(g),ports=g.vertices.map(v=>[v.id,graphAddresses(v.id,g).map(a=>a.id)]);
  const moving=editing&&graphDrag?.moved?graphDrag.node.id:null;
  const key=JSON.stringify([g.routing_version,g.vertices,g.bindings,edges,ports,moving,editing]);
  if(graphConnectionCache?.key===key)return graphConnectionCache.routes;
  const groups=new Map(),used=[],routes=edges.map(([a,b],i)=>({a,b,i}));
  for(const r of routes)for(const [side,id] of [['out',r.a],['in',r.b]]){
    const v=g.vertices.find(v=>v.id===id),address=graphPortAddress(g,v,side,[r.a,r.b]);
    const point=graphPortPoint(g,id,side,address,boxes),key=JSON.stringify([id,side,address]);
    if(!groups.has(key))groups.set(key,[]);
    groups.get(key).push({r,side,point});
  }
  for(const group of groups.values()){
    // Sort by the other endpoint's position, never by current selection.
    group.sort((a,b)=>{
      const x=boxes.get(a.side==='out'?a.r.b:a.r.a),y=boxes.get(b.side==='out'?b.r.b:b.r.a);
      return x.y-y.y||x.x-y.x||a.r.i-b.r.i;
    });
    const spacing=Math.min(9,18/Math.max(1,group.length-1));
    group.forEach((p,i)=>{
      if(editing)p.point[0]+=p.side==='out'?10:-12;
      p.point[1]+=(i-(group.length-1)/2)*spacing;
      p.r[p.side]={point:p.point,spacing};
    });
  }
  const ordered=[...routes].sort((a,b)=>boxes.get(a.a).x-boxes.get(b.a).x||boxes.get(a.a).y-boxes.get(b.a).y||a.i-b.i);
  // Keep unrelated wires steady during a drag. Route the whole canvas once
  // again on release, including wires which now need to avoid the moved card.
  if(moving)for(const r of ordered){
    const prior=graphConnectionCache?.routes[r.i];
    if(r.a!==moving&&r.b!==moving&&prior?.a===r.a&&prior.b===r.b){
      Object.assign(r,prior);
      for(let i=1;i<r.points.length;i++)used.push([r.points[i-1],r.points[i]]);
    }
  }
  for(const r of ordered){
    if(r.d)continue;
    const spacing=Math.min(r.in.spacing,r.out.spacing);
    const points=graphRoutePoints(r.out.point,r.in.point,boxes.get(r.a),boxes.get(r.b),[...boxes.values()],r.i,used,Math.min(6,spacing*.8));
    r.points=points;r.d=graphRoundedPath(points);r.arrowHeight=Math.min(5,r.in.spacing*.65);
    for(let i=1;i<points.length;i++)used.push([points[i-1],points[i]]);
  }
  graphConnectionCache={key,routes};return routes;
}

function graphRuntimeLines(g,link,boxes) {
  const routes=graphRoutes(link),colors=graphEntryColors(g),entries=[...colors.keys()];
  if(graphSession?.kind==='choose'&&graphSession.prefix.length>1)routes.push({id:'preview',entry:graphSession.prefix[0],path:graphSession.prefix,graph:graphSession.graph,pending:true});
  const flowing=new Set();
  for(const r of routes){
    if(r.pending||r.state==='deleting'||!r.path.every(id=>{
      const v=g.vertices.find(v=>v.id===id),node=canvasView.nodes.find(n=>n.id===v?.node_id);
      return v&&!v.disabled&&(!v.node_id||node&&!['offline','error','skew','deleting','new'].includes(node.status));
    }))continue;
    for(let i=1;i<r.path.length;i++)flowing.add(JSON.stringify([r.entry,r.path[i-1],r.path[i]]));
  }
  const spans=new Map();
  const binding=(r,a,b)=>{
    const graph=r.graph||g;
    return ['from','to'].map((side,i)=>graph.bindings?.[`${a}/${b}`]?.[side]?.[i?b:a]||'');
  };
  const live=new Set(routes.filter(r=>!r.pending).flatMap(r=>r.path.slice(1).map((b,i)=>JSON.stringify([r.entry,r.path[i],b,...binding(r,r.path[i],b)]))));
  for(const r of routes)for(let i=1;i<r.path.length;i++){
    const a=r.path[i-1],b=r.path[i],addresses=binding(r,a,b),key=JSON.stringify([r.entry,a,b,...addresses,r.pending]);
    if(r.pending&&live.has(JSON.stringify([r.entry,a,b,...addresses])))continue;
    if(boxes.has(a)&&boxes.has(b))spans.set(key,{a,b,addresses,entry:r.entry,pending:r.pending});
  }
  const list=[...spans.values()].sort((a,b)=>entries.indexOf(a.entry)-entries.indexOf(b.entry)||Number(a.pending)-Number(b.pending)||a.a.localeCompare(b.a)||a.b.localeCompare(b.b));
  // Port rows use the live address inventory, not only the published snapshot.
  const ports=g.vertices.map(v=>graphAddresses(v.id,g).map(a=>a.id));
  const key=JSON.stringify([g.routing_version,g.vertices,g.bindings,list,ports]);
  if(graphRouteCache?.key!==key){
    const used=[],ports=new Map();
    // Allocate both ends by the actual IP port. Distinct source edges can still
    // converge on the same receiving port and must not reuse its arrow position.
    for(const span of list){
      const edge=[span.a,span.b];
      for(const [side,id] of [['out',span.a],['in',span.b]]){
        const address=span.addresses[side==='out'?0:1];
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
        const offset=(slots.indexOf(slot(c))-(slots.length-1)/2)*spacing;
        c.point[1]+=offset;
        c.span[c.side]={point:c.point,spacing,lane:slots.indexOf(slot(c)),offset};
      }
    }
    for(const span of list){
      const edge=[span.a,span.b];
      const start=span.out.point,end=span.in.point,lane=span.in.lane;
      const source=boxes.get(edge[0]),target=boxes.get(edge[1]);
      const obstacles=[...boxes.values()].filter(box=>box.id!==span.a&&box.id!==span.b);
      const all=[source,target,...obstacles.filter(box=>box.id!==source.id&&box.id!==target.id)];
      // Parallel routes need opposite bend offsets when travelling down/up;
      // shifting every bend to the right makes adjacent colours cross.
      const bend=-Math.sign(end[1]-start[1])*(span.out.offset+span.in.offset)/2;
      const points=graphRoutePoints(start,end,source,target,all,lane,used.filter(s=>s.entry!==span.entry||s.pending!==span.pending).map(s=>s.points),6,bend);
      span.points=points;span.d=graphRoundedPath(points);
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
    return `<g class="graph-route${muted}" data-runtime-route="${i}"><defs><marker id="${arrow}" viewBox="0 0 7 6" refX="7" refY="3" markerUnits="userSpaceOnUse" markerWidth="7" markerHeight="${r.arrowHeight}" preserveAspectRatio="none" orient="auto"><path d="M 0 0 L 7 3 L 0 6 Z" fill="context-stroke"/></marker></defs><path class="graph-route-halo" ${attrs}/><path class="graph-line graph-route-line ${r.pending?'graph-next':'graph-live'}" ${attrs} marker-end="url(#${arrow})"/>${!r.pending&&flowing.has(JSON.stringify([r.entry,r.a,r.b]))?`<path class="topology-flow graph-route-flow" ${attrs}/>`:''}</g>`;
  }).join('');
}

function graphRouteStatus(g,link) {
  if(!link?.configurations?.length)return '';
  const colors=graphEntryColors(g),vertex=id=>g.vertices.find(v=>v.id===id),name=id=>graphName(vertex(id));
  const rows=link.configurations.map(c=>{
    const entry=c.active_path[0]||c.entry,target=c.active_path.at(-1)||c.target;
    const path=c.active_path.length?(c.active_path.slice(1,-1).map(id=>graphLabel(vertex(id))).join(' → ')||'direct'):'等待生效';
    return `<div class="graph-route-status" data-config-route="${c.id}"><span><i class="graph-entry-dot" style="background:${colors.get(c.entry)}"></i>${graphLabel(vertex(entry))} → ${targetLabel(name(target),c.target_address)}</span><span class="graph-route-description" title="${esc(c.active_path.map(name).join(' → '))}">${path}</span>${linkState(c)}${c.error||c.failover?.message?`<small>${esc(c.error||c.failover.message)}</small>`:''}</div>`;
  }).join('');
  return detailsBlock('graph-paths-'+link.id,'路径状态',rows);
}

function graphClearFailurePreview() {
  clearTimeout(graphHintTimer);clearTimeout(graphHintClose);
  graphFailurePreview?.controller?.abort();graphFailurePreview=null;
  const popover=app.querySelector('[data-failover-popover]');if(popover){popover.hidden=true;popover.replaceChildren();}
  app.querySelector('[data-failover-lines]')?.replaceChildren();
  app.querySelectorAll('.graph-failover-node,.graph-failover-source').forEach(el=>{
    el.classList.remove('graph-failover-node','graph-failover-source');el.style.removeProperty('--preview-color');
  });
}

function graphQuickHint(id) {
  clearTimeout(graphHintClose);
  if(rendering||graphSession||graphBusy||!canvasView?.link||canvasView.link.state==='deleting')return graphClearFailurePreview();
  const g=graphDisplay(graphCurrent()),v=g.vertices.find(v=>v.id===id);
  if(!v||v.kind!=='node'){
    graphHintClose=setTimeout(graphClearFailurePreview,140);return;
  }
  if(graphFailurePreview?.id===id&&graphFailurePreview.linkId===canvasView.link.id)return;
  graphClearFailurePreview();
  const preview=graphFailurePreview={id,linkId:canvasView.link.id,revision:canvasView.link.revision,row:null,data:null,at:0};
  graphHintTimer=setTimeout(()=>graphLoadFailurePreview(preview),250);
}

async function graphLoadFailurePreview(preview) {
  if(graphFailurePreview!==preview||graphSession||!canvasView?.link)return;
  const controller=new AbortController(),page=pageVersion;
  preview.controller=controller;preview.started=true;preview.revision=canvasView.link.revision;preview.error='';
  graphPaintFailurePreview();
  const timer=setTimeout(()=>controller.abort(),5000);
  try{
    const data=await api('GET',`topologies/${preview.linkId}/failover-preview?vertex=${encodeURIComponent(preview.id)}&revision=${preview.revision}`,null,controller.signal);
    if(graphFailurePreview!==preview||page!==pageVersion||graphSession||canvasView?.link?.id!==preview.linkId)return;
    if(data.revision!==canvasView.link.revision)return;
    preview.data=data;preview.at=Date.now();
    if(!data.paths.some(r=>r.id===preview.row))preview.row=data.paths[0]?.id??null;
  }catch(e){
    if(graphFailurePreview===preview){preview.data=null;preview.error=e.status===409?'拓扑已变化，等待刷新':e.message;preview.at=Date.now();}
  }finally{
    clearTimeout(timer);
    if(graphFailurePreview===preview){preview.controller=null;graphPaintFailurePreview();}
  }
}

function graphRestoreFailurePreview() {
  const preview=graphFailurePreview;
  if(!preview)return;
  if(graphSession||canvasView?.link?.id!==preview.linkId||!graphCurrent().vertices.some(v=>v.id===preview.id))return graphClearFailurePreview();
  if(preview.revision!==canvasView.link.revision){
    preview.controller?.abort();
    const id=preview.id;graphClearFailurePreview();graphQuickHint(id);return;
  }
  if(!preview.started)return;
  graphPaintFailurePreview();
  // Refresh only an open preview, using the page's normal refresh cadence.
  if(!preview.controller&&preview.at&&Date.now()-preview.at>2000)graphLoadFailurePreview(preview);
}

function graphFailureMarkup(g) {
  const preview=graphFailurePreview;if(!preview)return '';
  const v=g.vertices.find(v=>v.id===preview.id);if(!v)return '';
  const data=preview.data,rows=data?.paths||[],colors=graphEntryColors(g);
  const name=id=>graphLabel(g.vertices.find(v=>v.id===id));
  return `<div class="graph-failover-heading"><strong>灾备预览</strong><span>${graphLabel(v)} 失联</span></div>${data?rows.length?`<div class="graph-failover-options">${rows.map(row=>`<button type="button" data-key="failover-${row.id}" data-failover-choice="${row.id}" class="${preview.row===row.id?'is-active':''}" aria-pressed="${preview.row===row.id}" style="--route-color:${colors.get(row.entry)}"><span><i class="graph-entry-dot"></i>${name(row.entry)} → ${targetLabel(graphName(g.vertices.find(v=>v.id===row.target)),row.target_address)}</span><small>${row.path.length?row.path.map(name).join(' → '):esc(row.message)}</small>${row.path.length?`<small>从 ${name(row.switch_at)} 切换 · 权重 ${graphWeight(row.weight)}</small>`:''}</button>`).join('')}</div><small class="graph-failover-foot">${preview.controller?'正在更新…':'按当前状态预计 · 指向条目查看路线'}</small>`:`<p>${esc(data.message)}</p>`:`<p>${esc(preview.error||'正在计算…')}</p>`}`;
}

function graphPaintFailurePreview() {
  const preview=graphFailurePreview,popover=app.querySelector('[data-failover-popover]');
  if(!preview||!popover||graphSession)return;
  const g=graphDisplay(graphCurrent()),v=g.vertices.find(v=>v.id===preview.id);
  const source=app.querySelector(`[data-graph-node="${preview.id}"]`),workspace=popover.parentElement;
  if(!v||!source)return graphClearFailurePreview();
  popover.hidden=false;
  const template=document.createElement('template');template.innerHTML=graphFailureMarkup(g);
  const wasRendering=rendering;rendering=true;
  try{morph(popover,template.content);}finally{rendering=wasRendering;}
  const rect=workspace.getBoundingClientRect(),anchor=source.getBoundingClientRect();
  let left=anchor.right-rect.left+10;
  if(left+popover.offsetWidth>rect.width-8)left=anchor.left-rect.left-popover.offsetWidth-10;
  popover.style.left=`${Math.max(8,Math.min(left,rect.width-popover.offsetWidth-8))}px`;
  popover.style.top=`${Math.max(8,Math.min(anchor.top-rect.top,rect.height-popover.offsetHeight-8))}px`;
  graphPaintFailurePath();
}

function graphPaintFailurePath() {
  const preview=graphFailurePreview,layer=app.querySelector('[data-failover-lines]');
  if(!preview||!layer)return;
  layer.replaceChildren();
  app.querySelectorAll('.graph-failover-node,.graph-failover-source').forEach(el=>{
    el.classList.remove('graph-failover-node','graph-failover-source');el.style.removeProperty('--preview-color');
  });
  const data=preview.data,row=data?.paths.find(r=>r.id===preview.row),g=graphDisplay(graphCurrent());
  app.querySelector(`[data-graph-node="${preview.id}"]`)?.classList.add('graph-failover-source');
  if(!row?.path.length)return;
  const color=graphEntryColors(g).get(row.entry),boxes=graphBoxes(g);
  const config=canvasView.link.configurations.find(c=>c.id===row.id),old=config?.active_graph;
  const unchanged=new Set(row.active_path.slice(1).map((b,i)=>`${row.active_path[i]}/${b}`).filter(key=>JSON.stringify(old?.bindings?.[key])===JSON.stringify(data.graph.bindings[key])));
  const used=(graphRouteCache?.list||[]).flatMap(r=>(r.points||[]).slice(1).map((p,i)=>[r.points[i],p]));
  const paths=[];
  for(const id of row.path){
    const el=app.querySelector(`[data-graph-node="${id}"]`);
    if(el){el.classList.add('graph-failover-node');el.style.setProperty('--preview-color',color);}
  }
  for(let i=1;i<row.path.length;i++){
    const a=row.path[i-1],b=row.path[i];if(unchanged.has(`${a}/${b}`))continue;
    if(!boxes.has(a)||!boxes.has(b))continue;
    const binding=data.graph.bindings[`${a}/${b}`];
    const start=graphPortPoint(g,a,'out',binding?.from?.[a]||'',boxes),end=graphPortPoint(g,b,'in',binding?.to?.[b]||'',boxes);
    // Preview arrows occupy a separate edge of the IP row from live arrows.
    start[1]+=11;end[1]+=11;
    const points=graphRoutePoints(start,end,boxes.get(a),boxes.get(b),[...boxes.values()],i,used,4);
    const d=graphRoundedPath(points);
    for(let j=1;j<points.length;j++)used.push([points[j-1],points[j]]);
    paths.push(`<path class="graph-failover-halo" d="${d}"/><path class="graph-failover-line" data-preview-from="${a}" data-preview-to="${b}" d="${d}" marker-end="url(#graph-failover-arrow)"/>`);
  }
  layer.innerHTML=`<defs><marker id="graph-failover-arrow" viewBox="0 0 7 6" refX="7" refY="3" markerUnits="userSpaceOnUse" markerWidth="6" markerHeight="4" orient="auto"><path d="M 0 0 L 7 3 L 0 6 Z" fill="${color}"/></marker></defs><g style="--preview-color:${color}">${paths.join('')}</g>`;
}

for(const event of ['mouseover','focusin','click'])document.addEventListener(event,e=>{
  const row=e.target.closest?.('[data-failover-choice]'),preview=graphFailurePreview;
  if(!row||!preview||preview.row===Number(row.dataset.failoverChoice))return;
  preview.row=Number(row.dataset.failoverChoice);
  // Keep focused/hovered buttons in place: only update their state and the SVG overlay.
  row.parentElement.querySelectorAll('[data-failover-choice]').forEach(el=>{
    const active=Number(el.dataset.failoverChoice)===preview.row;el.classList.toggle('is-active',active);el.setAttribute('aria-pressed',String(active));
  });
  graphPaintFailurePath();
});
document.addEventListener('pointerdown',e=>{if(!e.target.closest?.('[data-failover-popover]'))graphClearFailurePreview();});
document.addEventListener('wheel',e=>{if(e.target.closest?.('.graph-viewport'))graphClearFailurePreview();},{passive:true});
document.addEventListener('keydown',e=>{if(e.key==='Escape')graphClearFailurePreview();});
