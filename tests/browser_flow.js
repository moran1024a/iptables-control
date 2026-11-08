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
  await page.locator('[data-nav=settings]').click();await page.locator('text=0.6.0-rc.1').first().waitFor();
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
