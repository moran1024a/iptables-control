const {chromium}=require(process.env.PLAYWRIGHT_MODULE);
(async()=>{
 const browser=await chromium.launch({executablePath:process.env.TEST_BROWSER||'/opt/google/chrome/chrome',headless:true,args:['--no-sandbox']});
 const page=await browser.newPage({viewport:{width:1440,height:1000}});
 const errors=[];page.on('pageerror',e=>{errors.push(String(e));console.log('PAGE ERROR',String(e))});
 page.on('response',r=>{if(r.status()>=400)console.log('HTTP',r.status(),r.url())});
 try{
  await page.goto(process.env.TEST_URL);

  await page.locator('[name=username]').fill('admin');await page.locator('[name=password]').fill('admin_pass');
  await page.locator('form button.primary').click();
  await page.locator('[data-nav=nodes]').waitFor();
  await page.locator('[data-nav=routes]').click();
  await page.locator(`a[href="#/routes/${process.env.TEST_ROUTE}"]`).click();
  await page.locator('.graph-node').first().waitFor();


  // Exercise the global weight form without changing node identity or IP fields.
  await page.goto(process.env.TEST_URL+'#/nodes/1');
  await page.locator('[data-action=node-edit]').click();
  await page.locator('#modal input[name=weight]').fill('-5');
  await page.locator('#modal button.primary').click();
  await page.locator('#modal').waitFor({state:'hidden'});
  await page.goto(process.env.TEST_URL+'#/routes/'+process.env.TEST_ROUTE);
  await page.locator('[data-action=link-failover]').first().click();
  await page.waitForFunction(()=>!graphBusy&&canvasView.link.failover_enabled===1);
  await page.locator('[data-action=link-failover]').first().click();
  await page.waitForFunction(()=>!graphBusy&&canvasView.link.failover_enabled===0);
  await page.locator('[data-action=link-edit]').first().click();
  await page.evaluate(()=>ACTIONS['graph-edge']({dataset:{index:String(graphSession.graph.edges.findIndex(e=>e[0]==='v1'&&e[1]==='v3'))}}));
  await page.locator('[data-action=graph-insert-direct]').click();
  const direct=await page.evaluate(()=>graphSession.selected);
  await page.locator('[data-direct-weight]').fill('-7');
  await page.locator('[data-action=graph-check]').click();
  await page.waitForFunction(()=>!graphBusy);
  await page.locator('[data-action=graph-publish]').first().click();
  await page.waitForFunction(id=>!graphSession&&!graphBusy&&canvasView.link.configurations.some(c=>c.active_path.includes(id)),direct);
  await page.evaluate(id=>{
    const graph=canvasView.link.graph,node=graph.vertices.find(v=>v.id===id),next=graph.vertices.find(v=>v.id==='v3');
    if(node.weight!==-7||next.layer!==1)throw new Error('direct altered its downstream layer or lost its weight');
    if(canvasView.nodes.find(n=>n.id===1).weight!==-5)throw new Error('global weight form was not saved');
    if(app.querySelector(`[data-graph-node="${id}"]`).getBoundingClientRect().width>=app.querySelector('[data-graph-node="v3"]').getBoundingClientRect().width)throw new Error('direct must be a compact modifier');
    for(const path of app.querySelectorAll('.graph-line'))if(/NaN|undefined/.test(path.getAttribute('d')))throw new Error('invalid wire coordinates');
    const boxes=graphBoxes(graph);
    for(const [a,b] of graph.edges.filter(e=>e.includes(id))){
      const source=graph.vertices.find(v=>v.id===a),target=graph.vertices.find(v=>v.id===b);
      const start=graphPortPoint(graph,a,'out',graphPortAddress(graph,source,'out',[a,b]),boxes);
      const end=graphPortPoint(graph,b,'in',graphPortAddress(graph,target,'in',[a,b]),boxes);
      const line=app.querySelector(`.graph-live[data-graph-from="${a}"][data-graph-to="${b}"]`);
      if(line&&line.getBBox().y<Math.min(start[1],end[1])-12)throw new Error('short direct wire made an unnecessary detour');
    }
  },direct);
  if(process.env.TEST_CANVAS_SCREENSHOT)await page.screenshot({path:process.env.TEST_CANVAS_SCREENSHOT});
  await page.locator('[data-action=link-edit]').first().click();
  await page.locator(`[data-action=graph-select][data-id="${direct}"]`).click();
  await page.locator('[data-action=graph-remove]').click();
  await page.locator('[data-action=graph-publish]').first().click();
  await page.waitForFunction(id=>!graphSession&&!graphBusy&&!canvasView.link.view_graph.vertices.some(v=>v.id===id),direct);
  console.log('PASS global weights, direct editing, automatic recovery toggle');


  await page.locator('[data-action=link-edit]').first().click();
  await page.locator('[data-action=graph-add-open]').click();
  await page.locator('[data-action=graph-library-add][data-id="5"]').click();
  const extra=await page.evaluate(()=>graphSession.graph.vertices.find(v=>v.node_id===5).id);
  console.log('ADDED',extra);
  // An unconnected node is saveable, and an offline path cannot block saving.
  await page.locator('[data-action=graph-publish]').first().click();
  await page.waitForFunction(()=>!graphSession&&!graphBusy);
  console.log('SAVED',await page.locator('#toast').innerText());
  await page.locator('[data-action=link-edit]').first().click();
  // Drag a node, then connect D directly to target T1 using real IP ports.
  const node=page.locator(`[data-graph-node="${extra}"] [data-graph-drag]`);const box=await node.boundingBox();
  await page.mouse.move(box.x+35,box.y+20);await page.mouse.down();await page.mouse.move(box.x-60,box.y+90,{steps:8});await page.mouse.up();
  await page.locator('[data-action=graph-port][data-id="v4"][data-side=out][data-address="@auto"]').click();
  await page.locator('[data-action=graph-port][data-id="t1"][data-side=in]').click();
  await page.locator('[data-action=graph-publish]').first().click();
  await page.waitForFunction(()=>!graphSession&&!graphBusy);
  await page.locator('[data-action=graph-select][data-id="v4"]').click();
  await page.waitForFunction(()=>!graphBusy&&!graphSession);
  console.log('SWITCH',await page.locator('#toast').innerText());

  await page.locator('a[href^="#/quality/"]').first().click();
  await page.locator('[data-link-quality-range]').selectOption('604800');
  await page.waitForFunction(()=>document.querySelector('[data-link-quality-range]')?.value==='604800');
  console.log('QUALITY',await page.locator('#app h1').innerText());
  await page.locator('[data-nav=configs]').click();
  await page.locator('[data-action=config-rename]').first().click();

  await page.locator('#modal input[name=name]').fill('测试配置');await page.locator('#modal button.primary').click();
  await page.locator('#modal').waitFor({state:'hidden'});
  await page.locator('[data-nav=targets]').click();await page.locator('[data-action=target-edit]').first().waitFor();
  await page.locator('[data-nav=subscriptions]').click();
  console.log('SUBS',await page.locator('#app h1').innerText());
  await page.locator('[data-nav=settings]').click();await page.locator('text=0.6.0-rc.2').first().waitFor();
  await page.locator('[data-nav=routes]').click();
  await page.locator(`a[href="#/routes/${process.env.TEST_ROUTE}"]`).click();
  await page.locator('.graph-node').first().waitFor();
  // Confirmed and pending bindings must remain distinct even on the same edge.
  await page.evaluate(async()=>{
    const id=canvasView.link.id,config=canvasView.link.configurations.find(c=>c.active_path.length>2);
    const [entry,middle]=config.active_path,vertex=canvasView.link.graph.vertices.find(v=>v.id===middle);
    const node=canvasView.nodes.find(n=>n.id===vertex.node_id);
    await api('PATCH',`nodes/${node.id}`,{addresses:[...node.addresses,{id:'audit-alt',host:'10.23.0.2',primary:false}]});
    const latest=(await api('GET',`topologies/${id}`)).link,graph=latest.graph;
    graph.bindings[`${entry}/${middle}`].to[middle]='audit-alt';
    const paths=latest.configurations.filter(c=>c.active_path.length>1).map(c=>c.active_path);
    await api('POST',`topologies/${id}`,{graph,paths,name:latest.name,revision:latest.revision});
    await refresh();
    const live=app.querySelector(`.graph-live[data-graph-from="${entry}"][data-graph-to="${middle}"]`);
    const pending=app.querySelector(`.graph-next[data-graph-from="${entry}"][data-graph-to="${middle}"]`);
    if(!live||!pending||live.getAttribute('d')===pending.getAttribute('d'))throw new Error('IP change was not drawn as separate confirmed and pending wires');
    if(!app.querySelector('[data-config-route]'))throw new Error('Configuration path status is hidden');
    // Replacing a resource at the same canvas vertex must keep the old live card.
    const changed=(await api('GET',`topologies/${id}`)).link;
    const oldNode=vertex.node_id;
    changed.graph.vertices.find(v=>v.id===middle).node_id=5;
    changed.graph.bindings[`${entry}/${middle}`].to[middle]='main';
    await api('POST',`topologies/${id}`,{graph:changed.graph,name:changed.name,revision:changed.revision});
    await refresh();
    const active=canvasView.link.configurations.find(c=>c.id===config.id).active_path[1];
    const actual=graphCurrent().vertices.find(v=>v.id===active);
    if(active===middle||actual.node_id!==oldNode||!app.querySelector(`[data-graph-node="${active}"]`))throw new Error('Old live resource was replaced before confirmation');
  });
  console.log('PASS confirmed/pending canvas snapshots');
  if(errors.length)throw new Error(errors.join('\n'));
  console.log('PASS browser flow');
 }catch(e){console.error(e);if(process.env.TEST_SCREENSHOT)await page.screenshot({path:process.env.TEST_SCREENSHOT});console.log(await page.locator('#app').innerText());process.exitCode=1;}
 finally{await browser.close();}
})();
