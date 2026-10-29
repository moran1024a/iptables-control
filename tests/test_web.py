"""Exercise real DOM rendering, modes, white activity bars and target-only forms in Chrome."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from test_server import ROOT


class BrowserTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('google-chrome') or shutil.which('chromium'), 'Chrome is unavailable')
    def test_views_forms_and_passive_activity(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);chrome=shutil.which('google-chrome') or shutil.which('chromium')
            setup=r'''
const group={id:1,name:"默认分组",builtin:1};
const node={id:1,name:"passive node",addr:"192.0.2.1",ports:"10000-10020",kind:"forward",desired_mode:"passive",protocol_version:2,manual_required:true,first_seen:1,last_seen:2,group_id:1,status:"syncing",key:"ab".repeat(32),bar:{start:1,window:60,levels:["white","ok"],counts:[0,2],times:[null,2]},applied_generation:1,desired_generation:2};
const target={id:1,name:"target",type:"endpoint",host:"example.com",port:443,params:{},configs:[],group_id:1};
const ss={...target,id:2,type:"shadowsocks",name:"SS target",uri:"ss://example"};
const routeData={id:1,name:"route",node_ids:[1],nodes:[node],configs:[],group_id:1};
const chain={id:1,remark:"config",route_id:1,route_name:"route",target_id:1,target_name:"target",target_host:"example.com",target_port:443,group_id:1,hops:[{...node,node_id:1,port:10001}],pending:[{...node,node_id:1}],status:"applying",connection:{protocol:"endpoint",host:"192.0.2.1",port:10001,uri:"192.0.2.1:10001"}};
const responses={me:{username:"owner",version:"0.3.0"},nodes:{nodes:[node],groups:[group],now:100},"nodes/1":{node,rules:[],chains:[],events:[],sync_path:"/sync",public_url:"https://example.com",agent_url:"https://example.com/ictl.sh",forward_version:"0.2.0",now:100},routes:{routes:[routeData],groups:[group]},targets:{targets:[target,ss],groups:[group]},configs:{chains:[chain],nodes:[node],node_groups:[group],groups:[group]},settings:{components:{forward_agent:"0.2.0"},username:"owner",version:"0.3.0",sync_path:"/sync",panel_path:"/panel",public_url:""}};
const requests=[];
window.fetch=async (url,options={})=>{
 const path=String(url).split("api/")[1]?.split("?")[0];
 if(!path)throw Error("unexpected request "+url);
 if(options.method && options.method!=="GET") {requests.push({path,body:JSON.parse(options.body||"{}")});return {ok:true,status:200,json:async()=>({id:1,assigned:[],affected:0,key:null})};}
 if(!responses[path])throw Error("missing fixture "+path);
 return {ok:true,status:200,json:async()=>structuredClone(responses[path])};
};
window.addEventListener("error",e=>{document.querySelector('#result').textContent='FAIL '+e.message;});
'''
            exercise=r'''
(async()=>{
 const assert=(condition,message)=>{if(!condition)throw Error(message);};
 try {
  await new Promise(r=>setTimeout(r,100));
  leaveView(); await show(nodeList,false);
  assert(document.querySelector('.lv-white'),"passive white cell missing");
  assert(document.querySelector('.lv-ok').title.includes('2 次'),"activity count missing");
  assert(app.textContent.includes('被动')&&app.textContent.includes('等待中'),"passive status missing");
  const timeline=document.createElement('div');
  timeline.innerHTML=bar({start:0,window:20,levels:Array(60).fill('ok')});
  assert(timeline.querySelector('.bar-axis').textContent.includes('20 分钟前'),"active bar range wrong");
  assert(timeline.querySelector('.bar span').title.startsWith(`${clock(0)} – ${clock(20)}`),"active cell duration wrong");
  timeline.innerHTML=bar({start:0,window:60,levels:Array(60).fill('white'),counts:Array(60).fill(0)});
  assert(timeline.querySelector('.bar-axis').textContent.includes('60 分钟前'),"passive bar range changed");
  assert(timeline.querySelectorAll('.lv-white').length===60,"passive cell count changed");
  await nodeForm(node);
  assert(document.querySelector('[name=desired_mode]').value==='passive',"mode selection wrong");closeModal();
  await show(r=>nodeDetail(1,r),false);
  assert(app.textContent.includes('sudo ictl --sync'),"manual action hint missing");
  await show(targetList,false);assert(app.textContent.includes('SS target'),"target list missing");
  targetForm(target);assert(document.querySelector('[name=host]'),"endpoint form missing");closeModal();
  targetForm(ss);assert(document.querySelector('[name=uri]'),"import form missing");closeModal();
  await show(routeList,false);assert(app.textContent.includes('passive node'),"route lost");
  await show(chainList,false);await chainForm(chain);
  let form=document.querySelector('#modal form');
  assert(form.elements.target_id,"target selector missing");
  assert(!form.elements.target_host&&!form.elements.target_type&&!form.elements.tunnel_id,"inline target still present");
  form.elements.route_id.value='';form.elements.route_id.dispatchEvent(new Event('change'));
  assert(!form.querySelector('[name=config_port]'),"direct target allocates ports");
  await FORMS.chain(form,Object.fromEntries(new FormData(form)));
  assert(requests.at(-1).body.target_id===1&&requests.at(-1).body.route_id===null,"wrong config payload");
  leaveView();await show(settingsPage,false);assert(!app.textContent.includes('sing-box'),"obsolete core settings");
  assert(!document.querySelector('a[href="#/tunnels"]'),"obsolete nav");
  document.querySelector('#result').textContent='PASS';
 }catch(e){document.querySelector('#result').textContent='FAIL '+e.stack;}
})();
'''
            html=(ROOT/'server/web/index.html').read_text().replace('<script src="app.js"></script>',
                '<pre id="result">RUNNING</pre><script>'+setup+'</script><script>'+(ROOT/'server/web/app.js').read_text()+'</script><script>'+exercise+'</script>')
            html=html.replace('<link rel="stylesheet" href="style.css">','<style>'+(ROOT/'server/web/style.css').read_text()+'</style>')
            (root/'index.html').write_text(html)
            r=subprocess.run([chrome,'--headless','--no-sandbox','--disable-gpu','--disable-background-networking',
                '--no-first-run','--no-default-browser-check','--disable-dev-shm-usage',f'--user-data-dir={root}/profile',
                '--virtual-time-budget=5000','--dump-dom',(root/'index.html').as_uri()],capture_output=True,text=True,timeout=30)
            self.assertEqual(r.returncode,0,r.stderr[-3000:])
            result=r.stdout.split('<pre id="result">')[-1].split('</pre>')[0]
            self.assertEqual(result,'PASS',result[-4000:])
