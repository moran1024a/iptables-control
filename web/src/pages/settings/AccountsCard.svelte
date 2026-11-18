<script lang="ts">
  import Plus from '@lucide/svelte/icons/plus';
  import { get, patch, post } from '$lib/api';
  import { confirm } from '$lib/confirm.svelte';
  import { FieldError, FormState } from '$lib/form.svelte';
  import { dateTime, gigabytes } from '$lib/format';
  import { usePoll } from '$lib/poll.svelte';
  import { MODE_LABEL } from '$lib/status';
  import { toast } from '$lib/toast.svelte';
  import type { TrafficQuota } from '$lib/types';
  import Alert from '$ui/Alert.svelte';
  import Badge from '$ui/Badge.svelte';
  import Button from '$ui/Button.svelte';
  import Card from '$ui/Card.svelte';
  import CopyButton from '$ui/CopyButton.svelte';
  import Field from '$ui/Field.svelte';
  import FormDialog from '$ui/FormDialog.svelte';
  import Input from '$ui/Input.svelte';

  type Account = { id: number; username: string; enabled: boolean; revision: number; node_ids: number[];
    expires_at: number | null; expiring: boolean;
    hidden_node_ids: number[];
    cleanup_nodes: number; config_count: number; max_configs: number | null; traffic: Record<number, TrafficQuota> };
  type Accounts = { accounts: Account[]; nodes: { id: number; name: string; addr: string; mode: keyof typeof MODE_LABEL }[] };
  const list = usePoll((signal) => get<Accounts>('accounts', { signal }), { interval: 5000 });
  const form = new FormState(['username', 'password', 'node_ids', 'hidden_node_ids', 'enabled', 'max_configs', 'node_limits', 'expires_at']);
  let open = $state(false);
  let original = $state<Account | null>(null);
  let showPassword = $state(false);
  let draft = $state({ username: '', password: '', enabled: true, node_ids: [] as number[],
    hidden_node_ids: [] as number[], max_configs: '', expiry: '', limits: {} as Record<number, string>, resets: [] as number[] });
  const expiryInput = (account: Account | null) => account?.expires_at ? dateTime(account.expires_at).replace(' ', 'T') : '';
  const hidden = $derived(draft.hidden_node_ids.filter((id) => draft.node_ids.includes(id)));
  const nodeChanges = $derived((list.data?.nodes ?? [])
    .filter((n) => n.mode === 'active' && draft.node_ids.includes(n.id) &&
      ((draft.limits[n.id] ?? '') !== String(original?.traffic[n.id]?.limit_gb ?? '') || draft.resets.includes(n.id))));
  const dirty = $derived(!!draft.password || draft.username !== (original?.username ?? '') ||
    draft.enabled !== (original?.enabled ?? true) || draft.node_ids.length !== (original?.node_ids.length ?? 0) ||
    draft.node_ids.some((id) => !original?.node_ids.includes(id)) ||
    hidden.length !== (original?.hidden_node_ids.length ?? 0) || hidden.some((id) => !original?.hidden_node_ids.includes(id)) ||
    draft.max_configs !== String(original?.max_configs ?? '') || draft.expiry !== expiryInput(original) || !!nodeChanges.length);

  function edit(account: Account | null) {
    form.clear();
    original = account;
    showPassword = false;
    draft = { username: account?.username ?? '', password: '', enabled: account?.enabled ?? true, node_ids: [...(account?.node_ids ?? [])],
      hidden_node_ids: [...(account?.hidden_node_ids ?? [])],
      max_configs: String(account?.max_configs ?? ''), expiry: expiryInput(account),
      limits: Object.fromEntries(Object.entries(account?.traffic ?? {}).map(([id, q]) => [id, String(q.limit_gb ?? '')])),
      resets: [] };
    open = true;
  }

  function grant(id: number, enabled: boolean) {
    draft.node_ids = enabled ? [...draft.node_ids, id] : draft.node_ids.filter((n) => n !== id);
  }

  function hideAddress(id: number, enabled: boolean) {
    draft.hidden_node_ids = enabled ? [...draft.hidden_node_ids, id] : draft.hidden_node_ids.filter((n) => n !== id);
  }

  function generatePassword() {
    draft.password = Array.from(crypto.getRandomValues(new Uint8Array(18)), (value) => value.toString(16).padStart(2, '0')).join('');
    showPassword = true;
  }

  function toggleReset(id: number) {
    draft.resets = draft.resets.includes(id) ? draft.resets.filter((n) => n !== id) : [...draft.resets, id];
  }

  function amount(value: string, maximum: number, field: string) {
    if (!value) return null;
    const n = Number(value);
    if (!/^[0-9]+$/.test(value) || n > maximum) throw new FieldError(`请输入 0～${maximum} 的整数，或留空`, field);
    return n;
  }

  async function save() {
    const stop = original?.enabled && !draft.enabled;
    const revoke = original?.node_ids.some((id) => !draft.node_ids.includes(id));
    if ((stop || revoke) && !(await confirm({
      title: stop ? '停用子账户？' : '撤销节点授权？',
      body: stop ? '该账户会立即退出登录，现有订阅链接失效，转发规则等待节点清理。' : '涉及撤权节点的链路将停用，已有转发规则等待节点清理。',
      confirmLabel: stop ? '停用账户' : '撤销授权',
      danger: true,
    }))) return;
    if (await form.run(() => {
      const expires = draft.expiry ? new Date(draft.expiry).getTime() / 1000 : null;
      if (expires !== null && !Number.isSafeInteger(expires)) throw new FieldError('到期时间无效', 'expires_at');
      const body = { username: draft.username.trim(), enabled: draft.enabled, node_ids: draft.node_ids,
        hidden_node_ids: hidden,
        ...(draft.expiry !== expiryInput(original) ? { expires_at: expires } : {}),
        max_configs: amount(draft.max_configs, 1_000_000, 'max_configs'),
        node_limits: nodeChanges.map(({ id }) => ({
          node_id: id, limit_gb: amount(draft.limits[id] ?? '', 1_000_000_000, 'node_limits'), reset: draft.resets.includes(id),
        })),
        ...(draft.password ? { password: draft.password } : {}) };
      return original ? patch(`accounts/${original.id}`, { ...body, revision: original.revision }) : post('accounts', body);
    })) {
      open = false;
      draft.password = '';
      toast.ok(original ? '子账户已更新' : '子账户已创建');
      void list.refresh();
    }
  }
</script>

<Card title="子账户">
  {#snippet actions()}
    <Button size="sm" variant="primary" onclick={() => edit(null)} disabled={!list.data}><Plus />新建子账户</Button>
  {/snippet}
  {#if list.error}<Alert tone="warn">{list.error}</Alert>{/if}
  {#if list.data?.accounts.length}
    <ul class="divide-y">
      {#each list.data.accounts as account (account.id)}
        <li class="flex flex-wrap items-center gap-2 py-2 first:pt-0 last:pb-0">
          <span class="break-all text-sm font-medium">{account.username}</span>
          <Badge tone={account.expiring ? 'warn' : account.enabled ? 'ok' : 'idle'}>{account.expiring ? '到期清理中' : account.enabled ? '已启用' : '已停用'}</Badge>
          {#if account.expires_at}<span class="text-xs text-fg-muted">{dateTime(account.expires_at)} 到期</span>{/if}
          <span class="text-xs text-fg-muted">{account.node_ids.length} 个节点{account.cleanup_nodes ? ` · ${account.cleanup_nodes} 个节点待清理` : ''}</span>
          <span class="text-xs text-fg-muted">配置 {account.config_count}{account.max_configs === null ? '' : ` / ${account.max_configs}`}</span>
          <Button size="sm" class="ml-auto" disabled={account.expiring} onclick={() => edit(account)}>编辑</Button>
        </li>
      {/each}
    </ul>
  {:else if list.data}
    <p class="text-sm text-fg-muted">还没有子账户。</p>
  {/if}
</Card>

<FormDialog bind:open title={original ? '编辑子账户' : '新建子账户'} {form} {dirty}
  submitDisabled={!dirty || !draft.username.trim() || (!original && !draft.password)} onsubmit={save}
  onclose={() => { draft.password = ''; }}>
  <Field label="用户名" error={form.errors.username}>
    {#snippet children({ id, invalid })}
      <Input {id} {invalid} required maxlength={32} bind:value={draft.username} autocomplete="off" spellcheck={false} />
    {/snippet}
  </Field>
  <Field label={original ? '重置密码' : '密码'} hint={original ? '留空时保留当前密码' : '8–128 位'} error={form.errors.password}>
    {#snippet children({ id, invalid })}
      <div class="flex flex-wrap gap-1.5">
        <Input {id} {invalid} type={showPassword ? 'text' : 'password'} required={!original} minlength={8} maxlength={128}
          bind:value={draft.password} autocomplete="new-password" class="flex-1" />
        <Button size="sm" onclick={generatePassword}>生成</Button>
        <Button size="sm" onclick={() => (showPassword = !showPassword)} disabled={!draft.password}>{showPassword ? '隐藏' : '显示'}</Button>
        {#if draft.password}<CopyButton text={draft.password} label="复制密码" />{/if}
      </div>
    {/snippet}
  </Field>
  <Field label="到期时间" hint="使用本地时间。留空长期有效；到期后自动清理转发并删除账户及其数据。" error={form.errors.expires_at}>
    {#snippet children({ id, invalid })}
      <Input {id} {invalid} type="datetime-local" step="1" bind:value={draft.expiry} />
    {/snippet}
  </Field>
  <Field label="配置数量上限" hint="C 和 E 合计，留空为不限" error={form.errors.max_configs}>
    {#snippet children({ id, invalid })}
      <Input {id} {invalid} inputmode="numeric" pattern="[0-9]+" maxlength={7} bind:value={draft.max_configs} />
    {/snippet}
  </Field>
  <label class="flex items-center gap-2 text-sm"><input type="checkbox" bind:checked={draft.enabled} />启用此账户</label>
  <fieldset class="min-w-0 rounded-md border p-2.5">
    <legend class="px-1 text-sm font-medium">可使用的节点</legend>
    {#if form.errors.node_ids}<p class="text-xs text-danger-text">{form.errors.node_ids}</p>{/if}
    {#if form.errors.hidden_node_ids}<p class="text-xs text-danger-text">{form.errors.hidden_node_ids}</p>{/if}
    {#if form.errors.node_limits}<p class="text-xs text-danger-text">{form.errors.node_limits}</p>{/if}
    {#if list.data?.nodes.length}
      <div class="max-h-60 divide-y overflow-y-auto">
        {#each list.data.nodes as node (node.id)}
          <div class="py-2">
            <label class="flex cursor-pointer items-center gap-2 text-sm">
              <input type="checkbox" checked={draft.node_ids.includes(node.id)} onchange={(event) => grant(node.id, event.currentTarget.checked)} />
              <span class="min-w-0 flex-1 break-all">{node.name}</span>
              <span class="text-xs text-fg-muted">{MODE_LABEL[node.mode]} · {node.addr}</span>
            </label>
            {#if draft.node_ids.includes(node.id)}
              <label class="mt-1.5 ml-5 flex w-fit cursor-pointer items-center gap-1.5 text-xs text-fg-muted">
                <input type="checkbox" aria-label={`隐藏 ${node.name} 地址`} checked={hidden.includes(node.id)}
                  onchange={(event) => hideAddress(node.id, event.currentTarget.checked)} />隐藏地址（仅中转）
              </label>
            {/if}
            {#if node.mode === 'active' && draft.node_ids.includes(node.id)}
              {@const quota = original?.traffic[node.id]}
              <div class="mt-1.5 ml-5 flex flex-wrap items-center gap-2">
                <Input aria-label={`${node.name} 流量额度（GB）`} class="max-w-32" placeholder="不限（GB）"
                  inputmode="numeric" pattern="[0-9]+" maxlength={10} value={draft.limits[node.id] ?? ''}
                  oninput={(event) => (draft.limits[node.id] = event.currentTarget.value)} />
                <span class="text-xs {quota?.exhausted ? 'text-warn-text' : 'text-fg-muted'}">
                  {draft.resets.includes(node.id) ? '保存时重置用量' : `已用 ${gigabytes(quota?.used ?? '0')}`}
                </span>
                {#if quota && quota.used !== '0'}
                  <Button size="sm" variant="ghost" onclick={() => toggleReset(node.id)}>{draft.resets.includes(node.id) ? '取消重置' : '重置用量'}</Button>
                {/if}
              </div>
            {/if}
          </div>
        {/each}
      </div>
    {:else}
      <p class="text-xs text-fg-muted">暂无可分配节点。</p>
    {/if}
  </fieldset>
</FormDialog>
