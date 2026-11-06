import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))

"""Exercise real DOM rendering, modes, white activity bars and target-only forms in Chrome."""
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest
from test_server import ROOT
import network_quality as q


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
            def sample(slot, rtt=20, path='same', status='ok'):
                return dict(slot=slot, rtt=rtt if status == 'ok' else None, path_key=path,
                            path_label=path, resolved_ip='192.0.2.1', status=status, error='')
            histories = {}
            histories['loaded'] = dict(now=900, seconds=900, series=[dict(protocol='tcp-connect-v1',
                **q.summarize([dict(sample(t), epoch='loaded', previous_slot=t-28 if t else None)
                               for t in range(0, 901, 28)], 900, 900))])
            for seconds in q.HISTORY_STEPS:
                histories[str(seconds)] = dict(now=seconds, seconds=seconds, series=[dict(protocol='tcp-connect-v1',
                    **q.summarize([sample(t) for t in range(0, seconds + 1, 12)], seconds, seconds))])
                histories['sparse'+str(seconds)] = dict(now=3600, seconds=seconds, series=[dict(protocol='tcp-connect-v1',
                    **q.summarize([sample(t, status='timeout' if t % 300 == 150 else 'ok') for t in range(0, 3601, 10)], seconds, 3600))])
            for name, rows in dict(switch=[sample(3540, 200, 'old'), sample(3550, 20, 'new'), sample(3560, 25, 'old')],
                                   failure=[sample(3540), sample(3550, status='timeout'), sample(3560, 30)],
                                   outage=[sample(3540, 10), sample(3580, 30)]).items():
                histories[name] = dict(now=3600, seconds=3600, series=[dict(protocol='tcp-connect-v1', **q.summarize(rows, 3600, 3600))])
            setup=r'''
const reportResult=window.fetch.bind(window);
const resultObserver=new MutationObserver(()=>{
 const result=document.querySelector('#result').textContent;
 if(result==='RUNNING')return;
 resultObserver.disconnect();
 reportResult('/result',{method:'POST',body:result});
});
resultObserver.observe(document.querySelector('#result'),{childList:true,subtree:true,characterData:true});
const group={id:1,name:"默认分组",builtin:1};
const node={id:1,name:"passive node",addr:"192.0.2.1",ports:"10000-10020",kind:"forward",desired_mode:"passive",protocol_version:2,manual_required:true,first_seen:1,last_seen:2,group_id:1,status:"syncing",key:"ab".repeat(32),bar:{start:1,window:60,levels:["white","ok"],counts:[0,2],times:[null,2]},applied_generation:1,desired_generation:2};
const target={id:1,name:"target",type:"endpoint",host:"example.com",port:443,params:{},configs:[],group_id:1};
const ss={...target,id:2,type:"shadowsocks",name:"SS target",uri:"ss://example"};
const routeData={id:1,name:"route",node_ids:[1],nodes:[node],configs:[],group_id:1};
const chain={id:1,remark:"config",route_id:1,route_name:"route",target_id:1,target_name:"target",target_host:"example.com",target_port:443,group_id:1,hops:[{...node,node_id:1,port:10001}],pending:[{...node,node_id:1}],status:"applying",connection:{protocol:"endpoint",host:"192.0.2.1",port:10001,uri:"192.0.2.1:10001"}};
const responses={me:{username:"owner",version:"0.3.1"},nodes:{nodes:[node],groups:[group],now:100},"nodes/1":{node,rules:[],chains:[],events:[],sync_path:"/sync",public_url:"https://example.com",agent_url:"https://example.com/ictl.sh",forward_version:"0.2.1",now:100},routes:{routes:[routeData],groups:[group]},targets:{targets:[target,ss],groups:[group]},configs:{chains:[chain],nodes:[node],node_groups:[group],groups:[group]},settings:{components:{forward_agent:"0.2.1"},username:"owner",version:"0.3.1",sync_path:"/sync",panel_path:"/panel",public_url:""}};
const requests=[];
const pool={id:1,name:'香港池',members:[1,2],references:['主链路']};
const link={id:1,chain_id:1,applied_target_id:1,name:'主链路',revision:1,state:'active',error:'',job:null,cleanup:[],events:[],hops:[{node_id:3,name:'入口',port:10001},{node_id:1,name:'A',port:10002}],connection:chain.connection,selected:{p1:1},plan:{entry:3,port:10001,target:1,stages:[{id:'p1',kind:'pool',pool_id:1,default:1,order:[1,2],disabled:[],locked:null,direct:false}]}};
link.group_id=1;
const resources={groups:[group,{id:2,name:'第二组',builtin:0}],links:[link],pools:[pool],targets:[target],nodes:[{id:1,name:'A',eligible:true,status:'synced',mode:'active'},{id:2,name:'B',eligible:true,status:'synced',mode:'active'},{id:3,name:'入口',eligible:true,status:'synced',mode:'active'}]};
responses.configs.groups=resources.groups;
responses.subscriptions={public_url:'https://example.com',groups:[group],configs:[{id:1,name:'SS 配置',group_id:1,error:'',clash_error:''},{id:2,name:'普通地址',group_id:1,error:'缺少代理协议',clash_error:'缺少代理协议'}],subscriptions:[{id:1,name:'我的订阅',enabled:true,link_ids:[1],path:'/sub/test-token',counts:{clash:1,v2ray:1}}]};
responses.pools=resources;responses.links=resources;responses['links/1']={...resources,link};chain.link_id=1;chain.link_state='active';
window.fetch=async (url,options={})=>{
 const path=String(url).split("api/")[1]?.split("?")[0];
 if(!path)throw Error("unexpected request "+url);
 if(path==="quality") {
  const data={nodes:[{id:1,name:"A",mode:"active"},{id:2,name:"B",mode:"active"}],pair:{source:1,target:2,target_addr:"192.0.2.2",port:33445},now:1200,step:10,state:"collecting",adjacent:true,summary:{mean:20,peak:30,p95:30,jitter:10,samples:3,lost:1,loss:100/3},latest:{slot:1190,status:"timeout",error:"探测超时"},points:[{time:1180,mean:20,peak:30,loss:0,samples:1,lost:0,status:"ok",resolved_ip:"192.0.2.2"},{time:1190,mean:null,peak:null,loss:100,samples:1,lost:1,status:"timeout"},{time:1200,mean:null,peak:null,loss:null,samples:0,lost:0,status:"missing"}]};
  data.series = [{protocol:"udp-echo-v1",...data},{protocol:"icmp-echo-v1",...data,latest:{slot:1190,status:"ok",rtt:18},points:data.points.map(p=>({...p,mean:p.mean == null ? null : p.mean-2,peak:p.peak == null ? null : p.peak-2}))}];
  if(JSON.parse(options.body||'{}').scope==='links') {data.name='主链路';data.series.unshift({...data.series[0],protocol:'tcp-connect-v1'});data.sections=['B → C → 目标','B → C','C → 目标'].map((label,i)=>({id:String(i),kind:i?'子段':'全段',label,series:data.series}));}
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
  assert(app.querySelector('[name="quality-range"]').options.length===4,"time ranges missing");
  const gaps=qualityChart([{protocol:"udp-echo-v1",points:[{time:1180,mean:10,peak:10,status:"ok",resolved_ip:"a"},{time:1190,mean:null,status:"missing"},{time:1200,mean:20,peak:20,status:"ok",resolved_ip:"a"}]}],1200,900);
  assert(!gaps.includes('stroke-width="2"'),"chart joins missing samples");
  const changedPathChart=qualityChart([{protocol:'tcp-connect-v1',points:[{time:1190,mean:10,peak:10,status:'ok',resolved_ip:'a',path_key:'old'},{time:1200,mean:20,peak:20,status:'ok',resolved_ip:'a',path_key:'new'}]}],1200,900);
  assert(!changedPathChart.includes('stroke-width="2"')&&changedPathChart.includes('路径切换'),'chart joined different paths');
  const drawing=data=>{
    const t=document.createElement('template');t.innerHTML=qualityChart(data.series,data.now,data.seconds);return t.content;
  };
  const loadedGraph=drawing(qualityHistories.loaded);
  assert(loadedGraph.querySelectorAll('.quality-connection').length===loadedGraph.querySelectorAll('circle').length-1,'scheduler delays break the curve');
  for(const range of ['900','3600','86400','604800']) {
    const graph=drawing(qualityHistories[range]);
    assert(graph.querySelectorAll('.quality-connection').length===graph.querySelectorAll('circle').length-1,'delayed samples break '+range+' s curve');
    const data=qualityHistories['sparse'+range],sparse=drawing(data),lines=[...sparse.querySelectorAll('line[stroke-width="2"]')];
    assert(lines.length>0,'occasional timeouts reduce '+range+' s curve to dots');
    const width=sparse.querySelector('svg').viewBox.baseVal.width;
    for(const failure of data.series[0].points.filter(p=>p.status==='timeout')) {
      const x=QUALITY_PLOT.left+(failure.time-data.now+data.seconds)/data.seconds*(width-QUALITY_PLOT.left-QUALITY_PLOT.right);
      assert(!lines.some(line=>Number(line.getAttribute('x1'))<x-.1&&Number(line.getAttribute('x2'))>x+.1),'curve crosses a real timeout');
    }
  }
  assert([...drawing(qualityHistories['604800']).querySelectorAll('.quality-tick')].every(t=>/\d\d\/\d\d/.test(t.textContent)),'weekly ticks omit dates');
  const switched=drawing(qualityHistories.switch);
  assert(switched.querySelectorAll('circle').length===3&&!switched.querySelector('line[stroke-width="2"]'),'path switch is averaged or joined');
  assert(switched.querySelectorAll('line[stroke-dasharray="3 4"]').length===2,'switch markers missing');
  const failed=drawing(qualityHistories.failure);
  assert(failed.querySelectorAll('circle').length===2&&failed.querySelectorAll('.quality-failure').length===1&&!failed.querySelector('line[stroke-width="2"]'),'partial failure must split successful runs');
  assert(!drawing(qualityHistories.outage).querySelector('line[stroke-width="2"]'),'curve bridges a real outage');
  const bounded={now:1205,seconds:900,series:[{protocol:'tcp-connect-v1',points:[{time:310,mean:20,peak:20,status:'ok'}]}]};
  const labels=data=>[...drawing(data).querySelectorAll('svg > text')].map(t=>t.textContent).join('|');
  const scale=labels(bounded);
  bounded.series[0].points.unshift({time:300,mean:1000,peak:1000,status:'ok'});
  bounded.series[0].points.push({time:1210,mean:2000,peak:2000,status:'ok'});
  assert(labels(bounded)===scale&&drawing(bounded).querySelectorAll('circle').length===1,'offscreen samples change chart scale or escape plot');
  const outage=qualityHistories.outage;
  renderQuality(`<section class="quality-card" data-key="outage">${qualityChart(outage.series,outage.now,outage.seconds)}</section>`,[outage]);
  const outagePlot=app.querySelector('.quality-plot');showQualityPoint(outagePlot,3570);
  assert(outagePlot.qualityTime===3570&&outagePlot.querySelector('.quality-tooltip').textContent.includes('未采样')&&!outagePlot.querySelector('.quality-tooltip').textContent.includes('10.0 ms'),'gap tooltip reuses an old successful sample');
  const chartData=qualityHistories.switch;
  // Other protocols can have a single aggregate spanning this path switch.
  const combined={...chartData,series:[...chartData.series,{protocol:'udp-echo-v1',points:[{time:3540,end:3600,mean:18,peak:18,status:'ok'}]}]};
  const redraw=()=>renderQuality(`<section class="quality-card" data-key="drawing-test">${qualityChart(combined.series,combined.now,combined.seconds)}</section>`,[combined]);
  redraw();
  const plot=app.querySelector('.quality-plot'),qualitySvg=plot.querySelector('svg');
  showQualityPoint(plot,3550);
  assert(plot.querySelector('.quality-tooltip').textContent.includes('18.0 ms'),'shared cursor loses protocol aggregates with different timestamps');
  redraw();
  assert(plot.qualityTime===3550&&!plot.querySelector('.quality-tooltip').hidden,'refresh loses selected timestamp');
  const requestsBeforeResize=requests.length;
  for (const width of [360,740]) {
    app.style.width=width+'px';
    await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
    assert(Math.abs(qualitySvg.viewBox.baseVal.width-plot.clientWidth)<=1,`resize did not recompute chart width (${qualitySvg.viewBox.baseVal.width} / ${plot.clientWidth})`);
    assert(Math.abs(qualitySvg.getScreenCTM().a-1)<.02,'resize shrinks text instead of laying it out');
    assert(plot.qualityTime===3550&&!plot.querySelector('.quality-tooltip').hidden,'resize loses selected timestamp');
  }
  assert(requests.length===requestsBeforeResize,'resize requests fresh network data');
  qualitySvg.dispatchEvent(new KeyboardEvent('keydown',{key:'End'}));
  assert(plot.qualityTime===plot.qualityData.times.at(-1),'keyboard End failed');
  showQualityPoint(plot,3555);
  qualitySvg.dispatchEvent(new KeyboardEvent('keydown',{key:'ArrowLeft'}));assert(plot.qualityTime===3550,'left key from hovered interval skipped sample');
  showQualityPoint(plot,3555);
  qualitySvg.dispatchEvent(new KeyboardEvent('keydown',{key:'ArrowRight'}));assert(plot.qualityTime===3560,'right key from hovered interval skipped sample');
  qualitySvg.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape'}));
  assert(plot.querySelector('.quality-tooltip').hidden,'keyboard Escape failed');
  app.style.removeProperty('width');
  leaveView();assert(qualityResizeObserver===null,'quality resize observer leaked after leaving page');
  leaveView();await show(poolList,false);assert(app.textContent.includes('香港池'),'pool list missing');
  poolForm(pool);await ACTIONS['pool-up']({dataset:{index:'1'}});
  assert(templateEditor.members[0]===2,'template reorder failed');closeModal();
  await show(linkList,false);assert(app.querySelector('a[href="#/routes/1"]'),'link detail entry missing');
  await show(r=>linkDetail(1,r),false);assert(app.querySelector('.topology-pool'),'topology missing');
  assert(app.querySelectorAll('.topology-choice').length===2,'pool members must all be visible');
  assert(app.querySelectorAll('.topology-flow').length===2,'confirmed route must animate through both gaps');
  const canvas=app.querySelector('.topology-canvas'), svg=app.querySelector('.topology-cables');
  assert(canvas.offsetWidth===Number(svg.getAttribute('width')),'SVG and cards use different coordinates');
  const cable=svg.querySelector('.topology-candidate'), canvasRect=canvas.getBoundingClientRect();
  for(const [end,point] of [[false,cable.dataset.from],[true,cable.dataset.to]]) {
    const cardRect=canvas.querySelector(`[data-point="${point}"]`).getBoundingClientRect();
    const position=cable.getPointAtLength(end?cable.getTotalLength():0),scale=topologyViews.get(1).scale;
    assert(Math.abs(position.x-((end?cardRect.left:cardRect.right)-canvasRect.left)/scale)<1.5 && Math.abs(position.y-((cardRect.top+cardRect.bottom)/2-canvasRect.top)/scale)<1.5,'compact cards and cable endpoints are misaligned');
  }
  assert(app.querySelector('.topology-flow').getAttribute('d').split('C').length===2,'links must connect nodes directly');
  const camera=topologyViews.get(1), initialScale=camera.scale;
  ACTIONS['canvas-in']();
  assert(camera.scale>initialScale,'zoom control failed');
  const saved={...camera};await linkDetail(1,viewRequest);
  assert(camera.scale===saved.scale && camera.x===saved.x,'refresh reset the canvas');
  ACTIONS['canvas-fit']();assert(camera.auto && camera.scale===initialScale,'fit control failed');
  const button=app.querySelector('.topology-choice[data-member="2"]');
  const sent=requests.length;
  ACTIONS['topology-switch'](button);
  assert(requests.length===sent && document.querySelector('[data-form="quick-switch"]'),'switch must ask first');
  assert(document.querySelector('[data-form="quick-switch"] strong').textContent==='B','confirmation must identify target');
  assert(!document.querySelector('.switch-order'),'switch must not show a reordered list');
  poolView.link.revision=99;
  await FORMS['quick-switch']();
  assert(requests.at(-1).body.member===2 && requests.at(-1).body.revision===1,'confirmation must use captured revision');
  assert(app.querySelector('.topology-choice[data-member="1"]').classList.contains('on-route'),'request must not optimistically change current route');
  assert([...app.querySelectorAll('.topology-choice')].map(n=>n.dataset.member).join(',')==='1,2','switch reordered members');
  const during=structuredClone(link);during.state='preparing';during.job={phase:'prepare',selected:{p1:2}};
  let preview=document.createElement('div');preview.innerHTML=topology(during);
  assert(preview.querySelector('.topology-flow') && preview.querySelector('.topology-pending'),'prepare should show old and pending paths');
  during.state='waiting';during.job.phase='commit';preview.innerHTML=topology(during);
  assert(!preview.querySelector('.topology-flow') && preview.querySelector('.topology-pending'),'unknown result must stop green animation');
  const changed=structuredClone(link);changed.plan.target=999;preview.innerHTML=topology(changed);
  assert(!preview.querySelector('.topology-flow'),'new target must not be shown as already confirmed');
  const locked=structuredClone(link);locked.plan.stages[0].locked=1;preview.innerHTML=topology(locked);
  assert(preview.querySelector('.topology-choice[data-member="2"]').disabled,'locked member restriction missing');
  await show(r=>linkDetail(1,r),false);
  instanceForm('p1');ACTIONS['instance-direct']();
  assert(instance().order.includes(0),'direct rule missing');
  instance().order=[2,1,0];drawInstance(true);
  assert([...document.querySelector('#instance-default').options].map(x=>x.value).join(',')===',1,2,0','member selector must follow node panel order');
  assert(instance().order.join(',')==='2,1,0','selector must not change pool priorities');
  ACTIONS['instance-disable']({dataset:{member:'2'}});assert(instance().disabled.includes(2),'local disable missing');
  const instanceFormElement=document.querySelector('#modal form');await FORMS['instance-save'](instanceFormElement);
  assert(requests.at(-1).path==='links/1' && requests.at(-1).body.plan.stages[0].direct,'instance save wrong');
  linkForm(link);assert(document.querySelector('#modal [name=target]').value==='1','link target missing');
  document.querySelector('[data-action=modal-close]').click();assert(document.querySelector('#modal').hidden,'cancel broken');
  await show(configSummary,false);assert(app.textContent.includes('192.0.2.1:10001'),'connection summary missing');
  // Filtering is local and must not alter the selected form value.
  leaveView();await show(linkList,false);
  const search=app.querySelector('[data-collection-query]');
  search.value='no match';search.dispatchEvent(new Event('input',{bubbles:true}));
  assert(!app.querySelector('.collection-row'),'list search did not filter');
  search.value='';search.dispatchEvent(new Event('input',{bubbles:true}));
  assert(app.querySelector('.collection-row'),'list search did not reset');
  linkForm(link);
  assert([...document.querySelector('#stage-add').options].map(x=>x.value).join(',')===',p:1,n:1,n:2,n:3','all pools must precede nodes');
  let picker=document.querySelector('[name=target]').closest('.resource-picker');
  picker.open=true;const pickerSearch=picker.querySelector('input');
  pickerSearch.value='no match';pickerSearch.dispatchEvent(new Event('input',{bubbles:true}));
  assert(document.querySelector('[name=target]').value==='1','search changed the selected target');
  assert(!picker.querySelector('.resource-empty').hidden,'selector empty state missing');
  picker.open=false;
  const name=document.querySelector('#modal [name=name]');
  showFormError(document.querySelector('#modal form'),new ApiError(400,{field:'target_id',error:'请选择目标'}));
  assert(document.activeElement===picker.querySelector('summary'),'error did not focus target picker');
  assert(document.querySelector('.field-error').getClientRects().length,'target error is hidden');
  name.value='unsaved edit';
  const discarded=requestCloseModal();
  assert(document.querySelector('[data-discard=no]'),'unsaved guard missing');
  document.querySelector('[data-discard=no]').click();
  assert(!(await discarded)&&document.querySelector('#modal [name=name]').value==='unsaved edit','cancel lost form input');
  const discardAgain=requestCloseModal();document.querySelector('[data-discard=yes]').click();
  assert(await discardAgain,'discard did not resolve');
  assert(document.querySelector('#modal').hidden,'discard did not close');
  const canceled=confirmBox('Delete','test');
  document.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true}));
  assert((await canceled)===false,'Escape did not cancel confirmation');
  poolForm();document.querySelectorAll('[data-member-choice]').forEach(el=>el.checked=true);
  ACTIONS['pool-add']();assert(templateEditor.members.length===3,'multi-selection lost members');
  const listElement=document.querySelector('[data-reorder=pool]');
  listElement.prepend(listElement.lastElementChild);listElement.dispatchEvent(new Event('reordered',{bubbles:true}));
  assert(templateEditor.members[0]===3,'dragged DOM order not saved');closeModal();
  leaveView();await show(r=>linkDetail(1,r),false);
  ACTIONS['canvas-fullscreen']();assert(app.querySelector('.is-fullscreen'),'fullscreen failed');
  await linkDetail(1,viewRequest);assert(app.querySelector('.is-fullscreen'),'refresh lost fullscreen');
  ACTIONS['canvas-current']();assert(!topologyViews.get(1).auto,'focus current path failed');
  ACTIONS['canvas-fullscreen']();
  const hover=app.querySelector('.topology-choice[data-member="2"]');
  hover.dispatchEvent(new MouseEvent('mouseover',{bubbles:true}));
  assert(app.querySelector('.has-highlight') && app.querySelector('.is-focused'),'hover focus missing');
  leaveView();await show(r=>linkQualityPage(1,r),false);
  assert(app.querySelectorAll('.quality-chart').length===3,'automatic segment charts missing');
  assert(!app.querySelector('[name=quality-source]'),'segment page still asks for source');
  assert(app.textContent.includes('B → C → 目标'),'full path label missing');
  assert(app.querySelector('[data-link-quality-range] option[value="604800"]'),'link quality lacks weekly range');
  const failureBoxes=[...app.querySelectorAll('.quality-failure')].map(p=>p.getBBox());
  const tickBoxes=[...app.querySelectorAll('.quality-tick')].map(p=>p.getBBox());
  assert(failureBoxes.every(p=>tickBoxes.every(t=>p.y+p.height<t.y)),'failure markers overlap time labels');
  const qualityFetch=window.fetch;
  window.fetch=async(url,options)=>String(url).endsWith('api/quality')?{ok:false,status:404,json:async()=>({error:'链路不存在'})}:qualityFetch(url,options);
  await refresh();window.fetch=qualityFetch;
  assert(!app.querySelector('.quality-chart')&&app.textContent.includes('链路不存在')&&currentView===null&&qualityResizeObserver===null,'deleted link retains chart or polling');
  await show(configSummary,false);assert(!app.querySelector('[data-action=group-rename]'),'config groups must be read-only');
  await show(linkList,false);await ACTIONS['sort-start']({dataset:{kind:'links'}});
  assert(app.querySelector('.sort-card'),'link sort mode missing');await ACTIONS['sort-save']({dataset:{kind:'links'}});
  assert(requests.at(-1).path==='links/order','link order saved to wrong endpoint');
  await show(subscriptionList,false);assert(app.textContent.includes('我的订阅'),'subscription list missing');
  subscriptionView.configs.push({id:3,name:'另一配置',group_id:1,error:'',clash_error:''});
  subscriptionForm(responses.subscriptions.subscriptions[0]);
  assert(document.querySelector('[name=link_id][value="1"]').checked,'bound config missing');
  assert(document.querySelector('[name=link_id][value="2"]').disabled,'plain endpoint should not be selectable');
  const subForm=document.querySelector('#modal form'),all=subForm.querySelector('[data-subscription-all]');
  assert(all.indeterminate&&!all.checked,'partial selection state missing');
  all.click();
  assert(all.checked&&!all.indeterminate&&subForm.querySelectorAll('[name=link_id]:checked').length===2,'select all must skip disabled configs');
  all.click();assert(!subForm.querySelector('[name=link_id]:checked'),'deselect all failed');
  subForm.querySelector('[name=link_id][value="3"]').click();
  assert(all.indeterminate,'individual selection must update select-all state');
  all.click();
  await FORMS.subscription(subForm,Object.fromEntries(new FormData(subForm)));
  assert(requests.at(-1).path==='subscriptions/1' && requests.at(-1).body.link_ids.join(',')==='1,3','bulk selection was not saved');
  assert(!document.body.classList.contains('canvas-fullscreen'),'fullscreen body state leaked');
  leaveView();await show(settingsPage,false);assert(!app.textContent.includes('sing-box'),"obsolete core settings");
  assert(!document.querySelector('a[href="#/tunnels"]'),"obsolete nav");

  const normalFetch=window.fetch, tick=()=>new Promise(resolve=>setTimeout(resolve,30));
  const response=data=>({ok:true,status:200,json:async()=>data});
  leaveView();await show(poolList,false);poolForm({name:'new pool',members:[1,2]});
  const saves=[];
  window.fetch=async(url,options)=>options?.method==='POST'&&String(url).endsWith('api/pools')
    ?await new Promise(resolve=>saves.push({resolve,body:JSON.parse(options.body)})):normalFetch(url,options);
  let editing=document.querySelector('#modal form');editing.requestSubmit();await tick();
  assert(editing.inert&&modalState.busy,'saving form must lock all controls');
  document.querySelector('[data-action=pool-down]:not(:disabled)').click();editing.requestSubmit();
  assert(saves.length===1&&templateEditor.members.join(',')==='1,2','saving can be reordered or submitted twice');
  assert(!(await requestCloseModal()),'pending save can be accidentally closed');
  window.fetch=normalFetch;saves[0].resolve(response({id:1}));await tick();
  assert(document.querySelector('#modal').hidden,'successful save did not close');

  // Bound both fetch and body reads; a timeout must leave the form editable.
  poolForm({name:'keep on timeout',members:[1]});editing=document.querySelector('#modal form');
  const nativeTimeout=window.setTimeout;
  window.setTimeout=(fn,ms,...args)=>nativeTimeout(fn,ms===20000?10:ms,...args);
  window.fetch=async(url,options)=>({ok:true,status:200,json:()=>new Promise((resolve,reject)=>options.signal.addEventListener('abort',()=>reject(new DOMException('aborted','AbortError'))))});
  editing.requestSubmit();await tick();window.setTimeout=nativeTimeout;window.fetch=normalFetch;
  assert(!editing.inert&&!modalState.busy&&!editing.querySelector('.primary').disabled,'timeout left form locked');
  assert(editing.querySelector('[name=name]').value==='keep on timeout'&&editing.textContent.includes('超时'),'timeout lost form data or feedback');closeModal();

  leaveView();await show(subscriptionList,false);subscriptionForm(subscriptionView.subscriptions[0]);
  const subscriptionEditor=document.querySelector('#modal form');subscriptionEditor.elements.name.value='unsaved subscription';
  let confirmation=ACTIONS['subscription-reset'](document.querySelector('[data-action=subscription-reset]'));
  document.querySelector('[data-confirm="0"]').click();await confirmation;
  assert(document.querySelector('#modal form')===subscriptionEditor&&!subscriptionEditor.closest('.modal-box').inert&&subscriptionEditor.elements.name.value==='unsaved subscription','cancel reset lost original editor');
  confirmation=ACTIONS['subscription-delete'](document.querySelector('[data-action=subscription-delete]'));
  document.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true}));await confirmation;
  assert(document.querySelector('#modal form')===subscriptionEditor,'Escape from nested confirm closed editor');
  confirmation=ACTIONS['subscription-reset'](document.querySelector('[data-action=subscription-reset]'));
  document.querySelector('[data-confirm="1"]').click();await confirmation;
  assert(subscriptionEditor.elements.name.value==='unsaved subscription'&&!subscriptionEditor.inert,'successful reset erased unrelated edits');closeModal();

  leaveView();await show(r=>nodeDetail(1,r),false);
  let finishQuery;window.fetch=async(url,options)=>String(url).endsWith('/query')?await new Promise(resolve=>finishQuery=resolve):normalFetch(url,options);
  const querying=ACTIONS['node-query']({dataset:{kind:'inventory'}});await tick();
  assert(document.querySelector('#modal').textContent.includes('查询中'),'query has no pending feedback');
  closeModal();leaveView();await show(subscriptionList,false);subscriptionForm(subscriptionView.subscriptions[0]);
  const newerEditor=document.querySelector('#modal form');newerEditor.elements.name.value='new edit';
  window.fetch=normalFetch;finishQuery(response({data:{host:'old node'}}));await querying;
  assert(document.querySelector('#modal form')===newerEditor&&newerEditor.elements.name.value==='new edit','late query replaced newer editor');closeModal();

  const clipboardDescriptor=Object.getOwnPropertyDescriptor(navigator,'clipboard'),execCopy=document.execCommand;
  Object.defineProperty(navigator,'clipboard',{value:undefined,configurable:true});document.execCommand=()=>false;
  assert(!(await copy('test'))&&document.querySelector('#toast').classList.contains('error'),'failed copy reported success');
  document.execCommand=()=>true;assert(await copy('test'),'successful fallback copy failed');
  document.execCommand=execCopy;if(clipboardDescriptor)Object.defineProperty(navigator,'clipboard',clipboardDescriptor);else delete navigator.clipboard;

  leaveView();await show(r=>linkDetail(1,r),false);linkForm(poolView.link);
  document.querySelector('#modal [name=name]').value='unsaved link';
  window.fetch=async(url,options)=>options?.method==='PATCH'&&String(url).endsWith('api/links/1')
    ?{ok:false,status:409,json:async()=>({error:'链路已变化，请刷新后重试'})}:normalFetch(url,options);
  document.querySelector('#modal form').requestSubmit();await tick();
  assert(document.querySelector('[data-action=link-reload]'),'conflict has no recovery action');
  let reloading=ACTIONS['link-reload'](document.querySelector('[data-action=link-reload]'));
  document.querySelector('[data-confirm="0"]').click();await reloading;
  assert(document.querySelector('#modal [name=name]').value==='unsaved link','cancel reload lost edits');
  link.revision+=1;window.fetch=normalFetch;
  reloading=ACTIONS['link-reload'](document.querySelector('[data-action=link-reload]'));
  document.querySelector('[data-confirm="1"]').click();await reloading;
  assert(linkEditor.revision===link.revision,'reload kept stale link revision');
  document.querySelector('#modal form').requestSubmit();await tick();
  assert(requests.filter(r=>r.path==='links/1').at(-1).body.revision===link.revision,'retry sent stale revision');
  // Saving a link may enqueue a hashchange; settle navigation before next view.
  await tick();closeModal();

  leaveView();await show(poolList,false);
  poolView.nodes.push({id:4,name:'passive choice',mode:'passive',eligible:false,status:'synced'},
    {id:5,name:'offline choice',mode:'active',eligible:true,status:'offline'});
  poolForm();
  assert(document.querySelector('[data-member-choice][value="4"]').disabled&&document.querySelector('#modal').textContent.includes('被动节点不能加入池'),'unavailable member has no reason');
  assert(!document.querySelector('[data-member-choice][value="5"]').disabled,'offline active node cannot be added to a template');
  closeModal();linkForm(link);
  assert(document.querySelector('[name=entry]').closest('.resource-picker').textContent.includes('离线'),'resource selector omits node state');closeModal();

  leaveView();await show(linkList,false);await ACTIONS['sort-start']({dataset:{kind:'links'}});
  const groupList=app.querySelector('.sort-list');groupList.prepend(groupList.lastElementChild);
  assert(sortChanged(),'changed group order was not detected');
  history.replaceState(null,'','#/routes');renderedHash='#/routes';history.replaceState(null,'','#/targets');
  let navigating=route();document.querySelector('[data-confirm="0"]').click();await navigating;
  assert(sortMode==='links'&&sortChanged()&&location.hash==='#/routes','cancel navigation lost ordering');
  history.replaceState(null,'','#/targets');navigating=route();document.querySelector('[data-confirm="1"]').click();await navigating;
  assert(sortMode===null&&app.querySelector('[data-action=target-new]'),'confirmed navigation did not leave sorting');
  leaveView();await show(linkList,false);app.style.width='360px';
  const controls=app.querySelector('.collection-tools');
  assert(getComputedStyle(controls).flexWrap==='wrap'&&controls.querySelector('input').getBoundingClientRect().bottom<=controls.querySelector('select').getBoundingClientRect().top,'mobile filters did not wrap');
  app.style.removeProperty('width');

  window.fetch=async(url,options)=>String(url).endsWith('api/links/999')?{ok:false,status:404,json:async()=>({error:'链路不存在'})}:normalFetch(url,options);
  leaveView();await show(r=>linkDetail(999,r),false);window.fetch=normalFetch;
  assert(app.textContent.includes('链路不存在')&&!app.querySelector('.collection-row')&&currentView===null,'missing link retained old page or refresh loop');
  leaveView();await show(poolList,false);expandedPools.clear();
  poolView.pools[0].members=[1,2,3,4,5];
  poolView.nodes.push({id:4,name:'第四个成员'},{id:5,name:'第五个成员'});drawCollection();
  assert(app.querySelectorAll('.member-tags > span').length===3,'pool preview shows all members by default');
  app.querySelector('[data-action=pool-expand]').click();
  assert(app.querySelectorAll('.member-tags > span').length===5 && app.textContent.includes('第五个成员'),'expanding pool loses members');
  await show(targetList,false);
  const actionMenu=app.querySelector('.target-row .more-actions');actionMenu.querySelector('summary').click();
  assert(actionMenu.open && actionMenu.querySelector('[data-action=target-delete]').getClientRects().length,'target actions are unreachable');
  document.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true}));
  assert(!actionMenu.open && document.activeElement===actionMenu.querySelector('summary'),'Escape did not dismiss action menu');
  document.querySelector('#result').textContent='PASS';
 }catch(e){document.querySelector('#result').textContent='FAIL '+e.stack;}
})();
'''
            setup='const qualityHistories='+json.dumps(histories)+';\n'+setup
            html=(ROOT/'server/web/index.html').read_text().replace('<script src="app.js"></script>',
                '<pre id="result">RUNNING</pre><script>'+setup+'</script><script>'+(ROOT/'server/web/app.js').read_text()+'</script><script>'+(ROOT/'server/web/pools.js').read_text()+'</script><script>'+(ROOT/'server/web/subscriptions.js').read_text()+'</script><script>'+exercise+'</script>')
            html=html.replace('<script src="pools.js"></script>','').replace('<script src="subscriptions.js"></script>','')
            html=html.replace('<link rel="stylesheet" href="style.css">','<style>'+(ROOT/'server/web/style.css').read_text()+'</style>')
            # Let actual frames run and report completion over local HTTP;
            # this does not depend on a browser debugging port or virtual time.
            done=threading.Event();results=[]
            class Handler(BaseHTTPRequestHandler):
                def log_message(self, *args):pass
                def do_GET(self):
                    if self.path!='/':self.send_error(404);return
                    self.send_response(200)
                    self.send_header('Content-Type','text/html; charset=utf-8')
                    self.end_headers();self.wfile.write(html.encode())
                def do_POST(self):
                    if self.path!='/result':self.send_error(404);return
                    results.append(self.rfile.read(int(self.headers['Content-Length'])).decode())
                    self.send_response(204);self.end_headers();done.set()
            with ThreadingHTTPServer(('127.0.0.1',0),Handler) as server, (root/'chrome.log').open('w') as log:
                thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
                process=subprocess.Popen([chrome,'--headless','--no-sandbox','--disable-gpu','--disable-background-networking',
                    '--no-first-run','--no-default-browser-check','--disable-dev-shm-usage','--window-size=500,900',f'--user-data-dir={root}/profile',
                    f'http://127.0.0.1:{server.server_port}/'],stdout=log,stderr=log)
                try:
                    self.assertTrue(done.wait(30),'Browser did not report a result:\n'+(root/'chrome.log').read_text()[-4000:])
                finally:
                    process.terminate()
                    try:process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill();process.wait()
                    server.shutdown();thread.join()
            self.assertEqual(results[0],'PASS',results[0][-4000:])
