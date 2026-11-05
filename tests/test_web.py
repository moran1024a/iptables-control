"""Exercise real DOM rendering, modes, white activity bars and target-only forms in Chrome."""
import os
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from test_server import ROOT


class InstallCommandTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), 'Node is unavailable')
    def test_download_does_not_follow_legacy_link_and_cleans_temporary_file(self):
        source=(ROOT/'server/web/app.js').read_text()
        code='\n'.join(line for line in source.splitlines() if line.startswith('  const quote =') or line.startswith('  const install ='))
        url="https://example.com/sync'quoted"
        command=subprocess.check_output(['node','-e','const d={agent_url:"https://example.com/ictl.sh"};const url='+json.dumps(url)+';\n'+code+'\nprocess.stdout.write(install("test-key"));'],text=True)
        for fail in ('0','1'):
            with self.subTest(download_failure=fail), tempfile.TemporaryDirectory() as directory:
                root=Path(directory);bin_dir=root/'bin';bin_dir.mkdir();tmp=root/'tmp';tmp.mkdir()
                installed=root/'installed';installed.write_text('installed Agent must remain unchanged')
                (root/'ictl.sh').symlink_to(installed)
                curl=bin_dir/'curl';curl.write_text('''#!/bin/bash
while [ "$#" -gt 0 ]; do
  if [ "$1" = -o ]; then output="$2"; shift; fi
  shift
done
printf '%s\\n' "$output" > "$MOCK_ROOT/download-path"
printf '%s\\n' '#!/bin/bash' 'printf "%s\\n" "$@" > "$MOCK_ROOT/arguments"' > "$output"
[ "$FAIL_DOWNLOAD" = 0 ]
''');curl.chmod(0o755)
                sudo=bin_dir/'sudo';sudo.write_text('#!/bin/bash\nexec "$@"\n');sudo.chmod(0o755)
                env={**os.environ,'PATH':str(bin_dir)+':'+os.environ['PATH'],'TMPDIR':str(tmp),'MOCK_ROOT':str(root),'FAIL_DOWNLOAD':fail}
                result=subprocess.run(['bash','-c',command],cwd=root,env=env,capture_output=True,text=True)
                self.assertEqual(result.returncode,0 if fail=='0' else 1,result.stderr)
                self.assertEqual(installed.read_text(),'installed Agent must remain unchanged')
                self.assertTrue((root/'ictl.sh').is_symlink())
                self.assertFalse(Path((root/'download-path').read_text().strip()).exists())
                if fail=='0':self.assertEqual((root/'arguments').read_text().splitlines(),['--url',url,'--key','test-key'])
                else:self.assertFalse((root/'arguments').exists())


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
const responses={me:{username:"owner",version:"0.3.1"},nodes:{nodes:[node],groups:[group],now:100},"nodes/1":{node,rules:[],chains:[],events:[],sync_path:"/sync",public_url:"https://example.com",agent_url:"https://example.com/ictl.sh",forward_version:"0.2.1",now:100},routes:{routes:[routeData],groups:[group]},targets:{targets:[target,ss],groups:[group]},configs:{chains:[chain],nodes:[node],node_groups:[group],groups:[group]},settings:{components:{forward_agent:"0.2.1"},username:"owner",version:"0.3.1",sync_path:"/sync",panel_path:"/panel",public_url:""}};
const requests=[];
const pool={id:1,name:'香港池',members:[1,2],references:['主链路']};
const link={id:1,name:'主链路',revision:1,state:'active',error:'',job:null,cleanup:[],events:[],hops:[{node_id:1,name:'入口'}],connection:chain.connection,selected:{p1:1},plan:{entry:1,port:10001,target:1,stages:[{id:'p1',kind:'pool',pool_id:1,order:[1,2],disabled:[],locked:null,direct:false}]}};
const resources={links:[link],pools:[pool],targets:[target],nodes:[{id:1,name:'A',eligible:true},{id:2,name:'B',eligible:true}]};
responses.pools=resources;responses.links=resources;responses['links/1']={...resources,link};chain.link_id=1;chain.link_state='active';
window.fetch=async (url,options={})=>{
 const path=String(url).split("api/")[1]?.split("?")[0];
 if(!path)throw Error("unexpected request "+url);
 if(path==="quality") {
  const data={nodes:[{id:1,name:"A",mode:"active"},{id:2,name:"B",mode:"active"}],pair:{source:1,target:2,target_addr:"192.0.2.2",port:33445},now:1200,step:10,state:"collecting",adjacent:true,summary:{mean:20,peak:30,p95:30,jitter:10,samples:3,lost:1,loss:100/3},latest:{slot:1190,status:"timeout",error:"探测超时"},points:[{time:1180,mean:20,peak:30,loss:0,samples:1,lost:0,status:"ok",resolved_ip:"192.0.2.2"},{time:1190,mean:null,peak:null,loss:100,samples:1,lost:1,status:"timeout"},{time:1200,mean:null,peak:null,loss:null,samples:0,lost:0,status:"missing"}]};
  data.series = [{protocol:"udp-echo-v1",...data},{protocol:"icmp-echo-v1",...data,latest:{slot:1190,status:"ok",rtt:18},points:data.points.map(p=>({...p,mean:p.mean == null ? null : p.mean-2,peak:p.peak == null ? null : p.peak-2}))}];
  return {ok:true,status:200,json:async()=>data};
 }
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
  node.installed_agent='active';node.backend='nftables';node.desired_mode='active';
  responses['nodes/1'].telemetry={metrics_at:100,inventory:{kernel:'test-kernel',arch:'amd64'},metrics:{cpu_percent:12.5},traffic:[{rule_id:1,protocol:'tcp',up:'18446744073709551615',down:'10',quality:'ok'}]};
  await show(r=>nodeDetail(1,r),false);
  assert(app.textContent.includes('test-kernel') && app.textContent.includes('转发累计流量'),"active telemetry missing");
  assert(document.querySelector('[title="18446744073709551615 字节"]'),"traffic integer lost precision");
  assert(document.querySelector('[data-action=node-query]'),"query actions missing");
  node.desired_mode='passive';
  node.status='error';node.err='端口冲突：TCP 10001 已被本机服务占用';
  await show(r=>nodeDetail(1,r),false);
  assert(app.textContent.includes('异常')&&app.textContent.includes(node.err),"conflict detail missing");
  chain.status='error';chain.pending[0].status='error';chain.pending[0].err=node.err;
  await show(chainList,false);
  assert(app.textContent.includes('异常')&&app.textContent.includes('等待确认'),"conflict cleared pending state");
  assert(app.querySelector('.who').title===node.err,"conflict source missing");
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
  leaveView();await show(r=>qualityPage("routes",1,r),false);
  assert(app.querySelectorAll('svg.quality-chart').length===1,"expected one combined quality chart");
  assert(app.textContent.includes('1 / 3 个样本'),"loss sample denominator missing");
  assert(app.textContent.includes('探测超时'),"timeout missing");
  assert(app.querySelector('svg [data-protocol="udp-echo-v1"] circle').getAttribute('fill') !== app.querySelector('svg [data-protocol="icmp-echo-v1"] circle').getAttribute('fill'),"protocol colours must differ");
  assert(!app.innerHTML.includes('NaN'),"chart contains NaN");
  assert(app.querySelector('[name="quality-range"]').options.length===3,"time ranges missing");
  const gaps=qualityChart([{protocol:"udp-echo-v1",points:[{time:1180,mean:10,peak:10,status:"ok",resolved_ip:"a"},{time:1190,mean:null,status:"missing"},{time:1200,mean:20,peak:20,status:"ok",resolved_ip:"a"}]}],1200,900);
  assert(!gaps.includes('stroke-width="2"'),"chart joins missing samples");
  leaveView();await show(poolList,false);assert(app.textContent.includes('香港池'),'pool list missing');
  poolForm(pool);await ACTIONS['pool-up']({dataset:{index:'1'}});
  assert(templateEditor.members[0]===2,'template reorder failed');closeModal();
  await show(linkList,false);assert(app.querySelector('a[href="#/routes/1"]'),'link detail entry missing');
  await show(r=>linkDetail(1,r),false);assert(app.querySelector('.topology-pool'),'topology missing');
  instanceForm('p1');ACTIONS['instance-direct']();
  assert(instance().order.includes(0),'direct rule missing');
  ACTIONS['instance-disable']({dataset:{member:'2'}});assert(instance().disabled.includes(2),'local disable missing');
  const instanceFormElement=document.querySelector('#modal form');await FORMS['instance-save'](instanceFormElement);
  assert(requests.at(-1).path==='links/1' && requests.at(-1).body.plan.stages[0].direct,'instance save wrong');
  linkForm(link);assert(document.querySelector('#modal [name=target]').value==='1','link target missing');
  document.querySelector('[data-action=modal-close]').click();assert(document.querySelector('#modal').hidden,'cancel broken');
  await show(configSummary,false);assert(app.textContent.includes('192.0.2.1:10001'),'connection summary missing');
  leaveView();await show(settingsPage,false);assert(!app.textContent.includes('sing-box'),"obsolete core settings");
  assert(!document.querySelector('a[href="#/tunnels"]'),"obsolete nav");
  document.querySelector('#result').textContent='PASS';
 }catch(e){document.querySelector('#result').textContent='FAIL '+e.stack;}
})();
'''
            html=(ROOT/'server/web/index.html').read_text().replace('<script src="app.js"></script>',
                '<pre id="result">RUNNING</pre><script>'+setup+'</script><script>'+(ROOT/'server/web/app.js').read_text()+'</script><script>'+(ROOT/'server/web/pools.js').read_text()+'</script><script>'+exercise+'</script>')
            html=html.replace('<script src="pools.js"></script>','')
            html=html.replace('<link rel="stylesheet" href="style.css">','<style>'+(ROOT/'server/web/style.css').read_text()+'</style>')
            (root/'index.html').write_text(html)
            r=subprocess.run([chrome,'--headless','--no-sandbox','--disable-gpu','--disable-background-networking',
                '--no-first-run','--no-default-browser-check','--disable-dev-shm-usage',f'--user-data-dir={root}/profile',
                '--virtual-time-budget=5000','--dump-dom',(root/'index.html').as_uri()],capture_output=True,text=True,timeout=30)
            self.assertEqual(r.returncode,0,r.stderr[-3000:])
            result=r.stdout.split('<pre id="result">')[-1].split('</pre>')[0]
            self.assertEqual(result,'PASS',result[-4000:])
