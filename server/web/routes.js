// One saved topology with independently confirmed configurations.
const LINK_STATES={active:'已生效',pending:'已保存',preparing:'准备下游',switching:'切换入口',waiting:'等待同步',error:'异常',deleting:'删除中'};
let canvasView=null;
const resourceName=(items,id,fallback='已删除')=>items.find(x=>x.id===id)?.name||fallback;
const memberName=id=>resourceName(canvasView.nodes,id);
const pageHead=(title,buttons='')=>`<div class="page-head"><h1>${title}</h1><div class="head-actions">${buttons}</div></div>`;
const linkStatus=l=>l.state;
const linkState=l=>`<span class="badge ${l.state==='error'?'removing':l.state==='active'?'applied':'applying'}">${LINK_STATES[l.state]||esc(l.state)}</span>`;
const collectionFilters = {topologies:{q:'',state:'',group:''}, configs:{q:'',state:'',group:''}};
filters.topologies=collectionFilters.topologies; filters.configs=collectionFilters.configs;
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

async function linkList(request) {
  const d=await api('GET','topologies');if(request!==viewRequest)return;canvasView=d;
  list={kind:'topologies',data:d,draw:()=>drawCollection()};
  drawCollection=()=>{
    const summary=l=>`${l.graph.vertices.filter(v=>v.kind==='entry').length} 个入口 · ${l.graph.vertices.filter(v=>v.kind==='target').length} 个目标 · ${l.configurations.length} 个配置`;
    if(sortMode==='topologies')return render(listHead('topologies',d.topologies.length,d.topologies.length)+sortList('topologies',d.topologies,l=>`<div class="title">${esc(l.name)}</div><div class="sub">${summary(l)}</div>`));
    const f=collectionFilters.topologies;
    const rows=d.topologies.filter(l=>matchesSearch('topologies',l.name)&&(!f.state||statusGroup(l)===f.state)&&(!f.group||l.group_id===Number(f.group)));
    const card=l=>`<a href="#/routes/${l.id}" class="collection-row link-card" data-key="topology-${l.id}"><div class="collection-main"><div class="title">${esc(l.name)}</div><div class="sub">${summary(l)}</div>${l.error?`<div class="row-error">${esc(l.error)}</div>`:''}</div>${l.cleanup.length?`<span class="muted">${l.cleanup.length} 项待清理</span>`:''}${linkState(l)}<span class="muted">›</span></a>`;
    render(listHead('topologies',d.topologies.length,rows.length,'link-new','新增链路')+collectionTools('topologies',rows.length,d.topologies.length,[['active','已生效'],['pending','等待同步'],['error','异常'],['deleting','删除中']],d.groups)+`<div class="collection">${groupedList('topologies',rows,card,()=> '链路')||emptyState('⇄','暂无链路')}</div>`);
  };drawCollection();
}
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
  if(l.state==='active'&&!l.warnings?.length)return '';
  const pending=l.configurations.filter(c=>c.state!=='active');
  return `<div class="sync-feedback" role="status">${pending.length?`<span>${pending.length} 项配置等待生效</span>`:''}${l.error?`<span class="row-error">${esc(l.error)}</span>`:''}${l.warnings?.length?`<span class="muted">${l.warnings.length} 个节点没有可达的入口—目标路径</span>`:''}${l.sync?.retry?'<button class="small" data-action="link-retry">重试同步</button>':''}</div>`;
}
document.addEventListener('keydown',e=>{
  if(e.key==='Escape' && $('#modal').hidden && topologyBinding?.view.fullscreen)topologyBinding.fullscreen();
  if((e.key==='Enter'||e.key===' ') && e.target.matches?.('.topology-hit')) {e.preventDefault();e.target.dispatchEvent(new MouseEvent('click',{bubbles:true}));}
});

async function linkQualityPage(id, request) {
  const view=qualityViews.get('topologies/'+id)||{},range=view.range||900;
  let d;
  try { d=await api('POST','quality',{scope:'topologies',id,range,...(view.configuration?{configuration:view.configuration}:{})}); }
  catch(err) {
    if(request!==viewRequest)return;
    if(err.field==='configuration'&&view.configuration){delete view.configuration;return linkQualityPage(id,request);}
    if(err.status!==404)throw err;
    leaveView();
    render('<h1>链路不存在</h1><p class="muted">该链路可能已被删除。</p><a href="#/routes">返回链路列表</a>');
    return;
  }
  if(request!==viewRequest)return;
  const cards=d.sections.map(section=>{
    const label=section.labels.map(item=>item.target_id?targetLabel(item.name,item.address):nodeLabel(item)).join(' → ');
    return `<section class="panel quality-card" data-key="quality-${section.id}"><div class="quality-heading"><h2><span class="quality-kind">${esc(section.kind)}</span> ${label}</h2>${qualityLegend(section.series)}</div>${qualityChart(section.series,d.now,range)}${qualityTable(section.series,d.now)}</section>`;
  }).join('');
  const configurations=d.configurations?.length>1?`<select data-link-quality-configuration="${id}" aria-label="入口与目标">${d.configurations.map(c=>`<option value="${c.id}" ${c.id===d.configuration?'selected':''}>${esc(c.name)}</option>`).join('')}</select>`:'';
  renderQuality(`<a class="back" href="#/routes/${id}">← ${esc(d.name)}</a>`+pageHead('网络质量',configurations+`<select data-link-quality-range="${id}" aria-label="时间范围">${QUALITY_RANGES.map(([n,label])=>`<option value="${n}" ${n===range?'selected':''}>${label}</option>`).join('')}</select>`)+cards,d.sections.map(section=>({series:section.series,now:d.now,seconds:range})));
}
document.addEventListener('change',e=>{
  if(e.target.hasAttribute('data-link-quality-configuration')){
    const key='topologies/'+e.target.dataset.linkQualityConfiguration;
    qualityViews.set(key,{...qualityViews.get(key),configuration:Number(e.target.value)});refresh();return;
  }
  if(!e.target.hasAttribute('data-link-quality-range'))return;
  const key='topologies/'+e.target.dataset.linkQualityRange;
  qualityViews.set(key,{...qualityViews.get(key),range:Number(e.target.value)});refresh();
});

async function configSummary(request) {
  const d=await api('GET','configs');if(request!==viewRequest)return;
  list={kind:'configs',data:d,draw:()=>drawCollection()};
  drawCollection=()=>{
    const f=collectionFilters.configs,route=c=>`${c.entry_name} → ${c.target_name}${targetAddressName(c.target_address)?' · '+targetAddressName(c.target_address):''}`;
    const rows=d.configs.filter(c=>matchesSearch('configs',[c.name,c.code,c.topology_name,route(c),c.connection?.host,...c.entry_tags.map(t=>t.name),c.target_address?.host,c.target_address?.port].join(' '))&&(!f.state||statusGroup(c)===f.state)&&(!f.group||c.group_id===Number(f.group)));
    const card=c=>`<div class="collection-row" data-key="config-${c.id}" data-config-id="${c.id}"><div class="collection-main inline-summary config-summary"><a class="title" href="#/routes/${c.topology_id}" title="${esc(c.name)}">${esc(c.name)}</a><small class="muted config-id">${esc(c.code)}</small><div class="sub"><a class="config-source" href="#/routes/${c.topology_id}" title="${esc(c.topology_name)}">链路：${esc(c.topology_name)}</a><code>${c.connection?esc(c.connection.host)+':'+c.connection.port:'等待入口确认'}</code><span class="config-path" title="${esc(route(c))}">${nodeLabel({name:c.entry_name,tags:c.entry_tags})} → ${targetLabel(c.target_name,c.target_address)}</span></div>${c.error?`<div class="row-error">${esc(c.error)}</div>`:''}</div>${linkState(c)}<div class="row-actions"><button class="small" data-action="config-rename" data-id="${c.id}">改名</button>${c.connection?`<button class="small" data-action="copy" data-text="${esc(c.connection.uri)}">复制${c.connection.protocol==='endpoint'?'地址':'导入链接'}</button>`:''}</div></div>`;
    render(pageHead('配置','<a class="button" href="#/routes">管理链路 →</a>')+collectionTools('configs',rows.length,d.configs.length,[['active','已生效'],['pending','等待同步'],['error','异常']],d.groups)+`<div class="collection">${groupedList('configs',rows,card,()=> '配置',false)||emptyState('⇄','暂无配置')}</div>`);
  };drawCollection();
}
function configRename(ident) {
  const config=list.data.configs.find(c=>c.id===ident);if(!config)return;
  openModal(`<form data-form="config-rename" data-id="${config.id}" data-previous-name="${esc(config.name)}"><h2>配置名称 <small class="muted config-id">${esc(config.code)}</small></h2><label><span>名称</span><input name="name" value="${esc(config.name)}" maxlength="64" required autocomplete="off"></label><div class="form-error" role="alert"></div><div class="form-actions"><button type="button" data-action="modal-close">取消</button><button class="primary">保存</button></div></form>`);
  $('#modal input[name=name]').select();
}
FORMS['config-rename']=async(form,data)=>{
  await api('PATCH',`configs/${form.dataset.id}/name`,{name:data.name,previous_name:form.dataset.previousName});
  closeModal();toast('配置名称已更新');await refresh();
};
Object.assign(ACTIONS,{
  'config-rename':el=>configRename(Number(el.dataset.id)),
  'canvas-in':()=>topologyBinding?.zoom(1.2),
  'canvas-out':()=>topologyBinding?.zoom(1/1.2),
  'canvas-fit':()=>topologyBinding?.fit(),
  'canvas-current':()=>topologyBinding?.focusCurrent(),
  'canvas-fullscreen':()=>topologyBinding?.fullscreen(),
  'edge-quality':()=>{location.hash=`#/quality/topologies/${canvasView.link.id}`;},
  'link-failover':()=>graphWork(async()=>{const l=canvasView.link;await api('POST',`topologies/${l.id}/action`,{action:'failover',enabled:!l.failover_enabled,revision:l.revision});toast(l.failover_enabled?'自动灾备已关闭':'自动灾备已开启');await refresh();}),
  async 'link-delete'(){if(!await confirmBox('删除链路',`删除 <b>${esc(canvasView.link.name)}</b> 并清理其转发规则？`,'删除',true))return;await api('DELETE',`topologies/${canvasView.link.id}`);location.hash='#/routes';},
  async 'link-retry'(){const l=canvasView.link;await api('POST',`topologies/${l.id}/action`,{action:'retry',revision:l.revision});toast('已重试同步');refresh();}
});
