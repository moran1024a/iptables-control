let subscriptionView = null;

async function subscriptionList(request) {
  const d=await api('GET','subscriptions'); if(request!==viewRequest)return;
  subscriptionView=d;
  const url=(row,format)=>(d.public_url||location.origin)+row.path+'?format='+format;
  render(pageHead('订阅','<button class="primary" data-action="subscription-new">新增订阅</button>')+
    `<div class="collection">${d.subscriptions.map(s=>`<div class="collection-row" data-key="subscription-${s.id}">
      <div class="collection-main inline-summary"><button class="plain-title" data-action="subscription-edit" data-id="${s.id}">${esc(s.name)}</button><div class="sub">${s.configuration_ids.length} 个配置 · Mihomo ${s.counts.clash} · v2rayN ${s.counts.v2ray}</div></div>
      <span class="badge ${s.enabled?'applied':'removing'}">${s.enabled?'已启用':'已禁用'}</span>
      <div class="row-actions"><button class="small" data-action="copy" data-text="${esc(url(s,'clash'))}" title="复制 Clash 订阅链接" aria-label="复制 Clash 订阅链接">Clash</button><button class="small" data-action="copy" data-text="${esc(url(s,'v2ray'))}" title="复制 v2rayN 订阅链接" aria-label="复制 v2rayN 订阅链接">v2rayN</button><button class="small" data-action="subscription-edit" data-id="${s.id}">编辑</button></div>
    </div>`).join('')||emptyState('⇅','暂无订阅')}</div>`);
}

function subscriptionForm(row) {
  const d=subscriptionView,ids=row?.configuration_ids||[];
  openModal(`<form data-form="subscription" data-id="${row?.id||''}"><h2>${row?'编辑订阅':'新增订阅'}</h2>
    <div class="form-row"><label><span>名称</span><input name="name" value="${esc(row?.name||'')}" maxlength="64" required></label>
    <label><span>状态</span><select name="enabled"><option value="1">启用</option><option value="0" ${row&&!row.enabled?'selected':''}>禁用</option></select></label></div>
    <div class="subscription-heading"><h3>绑定配置</h3><label><input type="checkbox" data-subscription-all data-ignore-dirty>全选</label></div><div class="subscription-choices">${d.groups.map(g=>{
      const items=d.configs.filter(c=>c.group_id===g.id);if(!items.length)return '';
      return `<div class="subscription-group">${d.groups.length>1?`<strong>${esc(g.name)}</strong>`:''}${items.map(c=>`<label><input type="checkbox" name="configuration_id" value="${c.id}" ${ids.includes(c.id)?'checked':''} ${c.error&&!ids.includes(c.id)?'disabled':''}><span>${esc(c.name)}${c.code?` <span class="muted config-id">${esc(c.code)}</span>`:''}${c.error||c.clash_error?`<small class="muted">${esc(c.error||c.clash_error+'；可用于 v2rayN')}</small>`:''}</span></label>`).join('')}</div>`;
    }).join('')||'<p class="muted">暂无配置，请先创建链路。</p>'}</div>
    <p class="hint">绑定配置随链路更新。VLESS Reality 请使用 Mihomo 内核客户端。</p><div class="form-error"></div>
    <div class="form-actions">${row?`<button type="button" class="danger" data-action="subscription-delete" data-id="${row.id}">删除</button><button type="button" data-action="subscription-reset" data-id="${row.id}">重置链接</button>`:''}<button type="button" data-action="modal-close">取消</button><button class="primary">保存</button></div></form>`);
  updateSubscriptionSelection($('#modal form'));
}

function updateSubscriptionSelection(form) {
  const all=form.querySelector('[data-subscription-all]'),items=[...form.querySelectorAll('[name=configuration_id]:not(:disabled)')];
  const selected=items.filter(x=>x.checked).length;
  all.disabled=!items.length;
  all.checked=items.length>0 && selected===items.length;
  all.indeterminate=selected>0 && selected<items.length;
}

document.addEventListener('change',e=>{
  const form=e.target.closest('[data-form="subscription"]');
  if(!form || !e.target.matches('[data-subscription-all],[name=configuration_id]'))return;
  if(e.target.hasAttribute('data-subscription-all'))form.querySelectorAll('[name=configuration_id]:not(:disabled)').forEach(x=>x.checked=e.target.checked);
  updateSubscriptionSelection(form);
});

FORMS.subscription=async(form,data)=>{
  const id=form.dataset.id;
  await api(id?'PATCH':'POST',id?`subscriptions/${id}`:'subscriptions',{
    name:data.name, enabled:data.enabled==='1', configuration_ids:[...form.querySelectorAll('[name=configuration_id]:checked')].map(x=>Number(x.value))
  });
  closeModal();toast('订阅已保存');await refresh();
};
Object.assign(ACTIONS,{
  'subscription-new':()=>subscriptionForm(),
  'subscription-edit':el=>subscriptionForm(subscriptionView.subscriptions.find(x=>x.id===Number(el.dataset.id))),
  async 'subscription-reset'(el){
    const id=el.dataset.id, form=el.closest('form');
    if(!await confirmBox('重置订阅链接','旧链接将立即失效，需要在客户端重新导入。','重置',true))return;
    await withBusyForm(form,async()=>{
      await api('PATCH',`subscriptions/${id}`,{reset_token:true});toast('链接已重置');await refresh();
    });
  },
  async 'subscription-delete'(el){
    const id=el.dataset.id, form=el.closest('form');
    if(!await confirmBox('删除订阅','订阅链接将失效，已绑定的链路保留。','删除',true))return;
    await withBusyForm(form,async()=>{
      await api('DELETE',`subscriptions/${id}`);closeModal();toast('订阅已删除');await refresh();
    });
  }
});
