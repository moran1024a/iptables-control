let subscriptionView = null;

async function subscriptionList(request) {
  const d=await api('GET','subscriptions'); if(request!==viewRequest)return;
  subscriptionView=d;
  const url=(row,format)=>(d.public_url||location.origin)+row.path+'?format='+format;
  render(poolHead('订阅','<button class="primary" data-action="subscription-new">新增订阅</button>')+
    `<div class="collection">${d.subscriptions.map(s=>`<div class="collection-row" data-key="subscription-${s.id}">
      <div class="collection-main"><button class="plain-title" data-action="subscription-edit" data-id="${s.id}">${esc(s.name)}</button><div class="sub">${s.link_ids.length} 个配置 · Mihomo ${s.counts.clash} · v2rayN ${s.counts.v2ray}</div></div>
      <span class="badge ${s.enabled?'applied':'removing'}">${s.enabled?'已启用':'已禁用'}</span>
      <div class="row-actions"><button class="small" data-action="copy" data-text="${esc(url(s,'clash'))}">复制 Clash 链接</button><button class="small" data-action="copy" data-text="${esc(url(s,'v2ray'))}">复制 v2rayN 链接</button><button class="small" data-action="subscription-edit" data-id="${s.id}">编辑</button></div>
    </div>`).join('')||emptyState('⇅','暂无订阅')}</div>`);
}

function subscriptionForm(row) {
  const d=subscriptionView,ids=row?.link_ids||[];
  openModal(`<form data-form="subscription" data-id="${row?.id||''}"><h2>${row?'编辑订阅':'新增订阅'}</h2>
    <label><span>名称</span><input name="name" value="${esc(row?.name||'')}" maxlength="64" required></label>
    <label><span>状态</span><select name="enabled"><option value="1">启用</option><option value="0" ${row&&!row.enabled?'selected':''}>禁用</option></select></label>
    <h3>绑定配置</h3><div class="subscription-choices">${d.groups.map(g=>{
      const items=d.configs.filter(c=>c.group_id===g.id);if(!items.length)return '';
      return `<div class="subscription-group">${d.groups.length>1?`<strong>${esc(g.name)}</strong>`:''}${items.map(c=>`<label><input type="checkbox" name="link_id" value="${c.id}" ${ids.includes(c.id)?'checked':''} ${c.error&&!ids.includes(c.id)?'disabled':''}><span>${esc(c.name)}${c.error||c.clash_error?`<small class="muted">${esc(c.error||c.clash_error+'；可用于 v2rayN')}</small>`:''}</span></label>`).join('')}</div>`;
    }).join('')||'<p class="muted">暂无配置，请先创建链路。</p>'}</div>
    <p class="hint">绑定配置随链路更新。VLESS Reality 请使用 Mihomo 内核客户端。</p><div class="form-error"></div>
    <div class="form-actions">${row?`<button type="button" class="danger" data-action="subscription-delete" data-id="${row.id}">删除</button><button type="button" data-action="subscription-reset" data-id="${row.id}">重置链接</button>`:''}<button type="button" data-action="modal-close">取消</button><button class="primary">保存</button></div></form>`);
}

FORMS.subscription=async(form,data)=>{
  const id=form.dataset.id;
  await api(id?'PATCH':'POST',id?`subscriptions/${id}`:'subscriptions',{
    name:data.name, enabled:data.enabled==='1', link_ids:[...form.querySelectorAll('[name=link_id]:checked')].map(x=>Number(x.value))
  });
  closeModal();toast('订阅已保存');await refresh();
};
Object.assign(ACTIONS,{
  'subscription-new':()=>subscriptionForm(),
  'subscription-edit':el=>subscriptionForm(subscriptionView.subscriptions.find(x=>x.id===Number(el.dataset.id))),
  async 'subscription-reset'(el){
    const id=el.dataset.id;
    if(!await confirmBox('重置订阅链接','旧链接将立即失效，需要在客户端重新导入。','重置',true))return;
    await api('PATCH',`subscriptions/${id}`,{reset_token:true});toast('链接已重置');await refresh();
  },
  async 'subscription-delete'(el){
    const id=el.dataset.id;
    if(!await confirmBox('删除订阅','订阅链接将失效，已绑定的链路保留。','删除',true))return;
    await api('DELETE',`subscriptions/${id}`);toast('订阅已删除');await refresh();
  }
});
