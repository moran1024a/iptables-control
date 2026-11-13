<script lang="ts">
  import { tick, untrack } from 'svelte';
  import { patch, post } from '$lib/api';
  import { FormState, snapshot } from '$lib/form.svelte';
  import { toast } from '$lib/toast.svelte';
  import type { Group, NodeCreate, NodeCreated, NodeDetail, NodeEdited, Tag } from '$lib/types';
  import Alert from '$ui/Alert.svelte';
  import Checkbox from '$ui/Checkbox.svelte';
  import Disclosure from '$ui/Disclosure.svelte';
  import Field from '$ui/Field.svelte';
  import FormDialog from '$ui/FormDialog.svelte';
  import Input from '$ui/Input.svelte';
  import Select from '$ui/Select.svelte';
  import Switch from '$ui/Switch.svelte';
  import TagBadge from '$ui/TagBadge.svelte';
  import Textarea from '$ui/Textarea.svelte';
  import AddressEditor from './AddressEditor.svelte';
  import { autofillDevices, initialRows, isPort, validateRows, type AddressRow, type RowErrors } from './node-logic';

  type Props = {
    open?: boolean;
    /** Edit this node; omit to create one. */
    node?: NodeDetail['node'] | null;
    groups: Group[];
    tags: Tag[];
    /** Called after a successful save with the node id. */
    onsaved: (id: number) => void;
  };
  let { open = $bindable(false), node = null, groups, tags, onsaved }: Props = $props();

  const MORE_FIELDS = ['note', 'ports', 'quality_port', 'wake_enabled', 'wake_port'];
  const form = new FormState(['name', 'group_id', 'tag_ids', 'weight', 'desired_mode', 'addr', 'addresses', ...MORE_FIELDS]);

  type Values = {
    name: string;
    group_id: number;
    tag_ids: number[];
    weight: number | null;
    desired_mode: 'active' | 'passive';
    rows: AddressRow[];
    note: string;
    ports: string;
    quality_port: number | null;
    wake_enabled: boolean;
    wake_port: number | null;
  };
  const blank = (): Values => ({
    name: '',
    group_id: 0,
    tag_ids: [],
    weight: 0,
    desired_mode: 'active',
    rows: [],
    note: '',
    ports: '10000-65535',
    quality_port: 33445,
    wake_enabled: false,
    wake_port: 0,
  });
  let v = $state<Values>(blank());
  let initial = $state('');
  let rowErrors = $state<Record<string, RowErrors>>({});
  let advanced = $state<Record<string, boolean>>({});
  let moreOpen = $state(false);

  // Start from the node (or the defaults) each time the dialog opens; later prop changes must not reset typed input.
  $effect(() => {
    if (!open) return;
    untrack(() => {
      const builtin = groups.find((group) => group.builtin)?.id ?? groups[0]?.id ?? 0;
      v = node
        ? {
            name: node.name,
            group_id: groups.some((group) => group.id === node.group_id) ? node.group_id : builtin,
            tag_ids: node.tags.map((tag) => tag.id),
            weight: node.weight,
            desired_mode: node.desired_mode,
            rows: [],
            note: node.note,
            ports: node.ports,
            quality_port: node.quality_port,
            wake_enabled: node.wake_enabled,
            wake_port: node.wake_port,
          }
        : { ...blank(), group_id: builtin };
      v.rows = autofillDevices(initialRows(node), node?.network);
      initial = snapshot(v);
      rowErrors = {};
      advanced = {};
      moreOpen = false;
      form.clear();
    });
  });

  const passive = $derived(v.desired_mode === 'passive');
  const modeChanged = $derived(!!node && v.desired_mode !== node.desired_mode);
  const primaryId = $derived(v.rows.find((row) => row.primary)?.id ?? '');
  // The server reports a bad primary address as `addr`; show it on that row.
  const shownRowErrors = $derived(form.errors.addr && primaryId ? { ...rowErrors, [primaryId]: { ...rowErrors[primaryId], host: form.errors.addr } } : rowErrors);

  function validate(): Record<string, string> {
    const errors: Record<string, string> = {};
    const name = v.name.trim();
    if (!name) errors.name = '名称不能为空';
    else if (name.length > 64) errors.name = '名称最长 64 个字符';
    if (typeof v.weight !== 'number' || !Number.isInteger(v.weight) || Math.abs(v.weight) > 100000) errors.weight = '权重须为 -100000 至 100000 的整数';
    if (v.note.length > 500) errors.note = '备注最长 500 个字符';
    if (!v.ports.trim()) errors.ports = '请填写允许的端口';
    else if (v.ports.trim().length > 300) errors.ports = '允许的端口最长 300 个字符';
    if (!isPort(v.quality_port)) errors.quality_port = '请填写 0-65535 的整数，0 表示关闭';
    if (passive) {
      if (!isPort(v.wake_port)) errors.wake_port = '请填写 0-65535 的整数';
      else if (v.wake_enabled && !v.wake_port) errors.wake_port = '启用远程唤醒需要填写端口';
    }
    const checked = validateRows(v.rows, node?.addresses);
    rowErrors = checked.rows;
    if (checked.list) errors.addresses = checked.list;
    return errors;
  }

  /** Open whatever hides a problem and move to the first one. */
  async function reveal() {
    if (MORE_FIELDS.some((field) => form.errors[field])) moreOpen = true;
    const next: Record<string, boolean> = { ...advanced };
    for (const [id, errors] of Object.entries(rowErrors)) if (errors.local || errors.device || errors.gateway) next[id] = true;
    // A server-side address error names no row: show every row's advanced values so nothing relevant stays hidden.
    if (form.errors.addresses) for (const row of v.rows) if (row.local || row.device || row.gateway) next[row.id] = true;
    advanced = next;
    await tick();
    const first = document.querySelector<HTMLElement>('[data-node-form] [aria-invalid="true"], [data-node-form] [role="alert"]');
    first?.scrollIntoView({ block: 'center' });
    if (first?.matches('input, select, textarea')) first.focus({ preventScroll: true });
  }

  async function submit() {
    const errors = validate();
    if (Object.keys(errors).length || Object.keys(rowErrors).length) {
      form.clear();
      form.errors = errors;
      await reveal();
      return;
    }
    const primary = v.rows.find((row) => row.primary)!;
    const body: NodeCreate = {
      addresses: v.rows.map((row) => ({
        id: row.id,
        primary: row.primary,
        host: row.host.trim(),
        name: row.name.trim(),
        local: row.local.trim(),
        device: row.device.trim(),
        gateway: row.gateway.trim(),
      })),
      tag_ids: v.tag_ids,
      weight: v.weight ?? 0,
      name: v.name,
      note: v.note,
      addr: primary.host.trim(),
      ports: v.ports,
      desired_mode: v.desired_mode,
      quality_port: v.quality_port ?? 0,
      // Wake only exists for passive nodes; the server forces it off otherwise.
      wake_enabled: passive && v.wake_enabled,
      wake_port: isPort(v.wake_port) ? (v.wake_port as number) : 0,
    };
    if (groups.length > 1) body.group_id = v.group_id;
    let id = node?.id ?? 0;
    let upstream: string[] = [];
    const ok = await form.run(async () => {
      if (node) upstream = (await patch<NodeEdited>(`nodes/${node.id}`, body)).upstream ?? [];
      else id = (await post<NodeCreated>('nodes', body)).id;
    });
    if (!ok) {
      await reveal();
      return;
    }
    open = false;
    if (node) {
      toast.ok('已保存');
      if (upstream.length) toast.info(`主 IP 已变更：上游节点 ${upstream.join('、')} 指向本节点的规则会随之更新`);
    }
    onsaved(id);
  }

  function toggleTag(id: number, checked: boolean) {
    v.tag_ids = checked ? [...v.tag_ids, id] : v.tag_ids.filter((other) => other !== id);
  }
</script>

<FormDialog bind:open title={node ? '编辑节点' : '新增节点'} size="lg" {form} dirty={snapshot(v) !== initial} onsubmit={submit}>
  <div class="flex flex-col gap-5" data-node-form>
    <div class="grid gap-x-4 gap-y-3 sm:grid-cols-2">
      <Field label="名称" error={form.errors.name} class={groups.length > 1 ? '' : 'sm:col-span-2'}>
        {#snippet children({ id, invalid })}
          <Input {id} {invalid} bind:value={v.name} maxlength={64} autofocus={!node} autocomplete="off" />
        {/snippet}
      </Field>
      {#if groups.length > 1}
        <Field label="分组" error={form.errors.group_id}>
          {#snippet children({ id, invalid })}
            <Select {id} {invalid} bind:value={v.group_id} options={groups.map((group) => ({ value: group.id, label: group.name }))} class="w-full" />
          {/snippet}
        </Field>
      {/if}
      <Field
        label="同步模式"
        error={form.errors.desired_mode}
        hint={passive ? '被动：配置变更后在节点上手动同步，可选远程唤醒。' : '主动：节点与面板保持长连接，自动同步。'}
      >
        {#snippet children({ id, invalid })}
          <Select
            {id}
            {invalid}
            bind:value={v.desired_mode}
            class="w-full"
            options={[
              { value: 'active', label: '主动（长连接自动同步）' },
              { value: 'passive', label: '被动（默认手动，可选远程唤醒）' },
            ]}
          />
        {/snippet}
      </Field>
      <Field label="权重" error={form.errors.weight} hint="0 为默认，越高越优先，作用于所有链路。">
        {#snippet children({ id, invalid })}
          <Input {id} {invalid} type="number" bind:value={v.weight} step={1} inputmode="numeric" class="tabular" />
        {/snippet}
      </Field>
      {#if node && (modeChanged || node.manual_required)}
        <Alert tone="warn" class="sm:col-span-2">
          {modeChanged ? '切换模式后' : '模式切换尚未确认，'}需在节点本机运行 <code class="font-mono">sudo ictl --sync</code> 确认{modeChanged ? '，确认前节点会显示为「等待中」' : ''}。
        </Alert>
      {/if}
    </div>

    <fieldset class="flex min-w-0 flex-col gap-1.5">
      <legend class="mb-1.5 text-[0.8667rem] font-medium">标签</legend>
      {#if tags.length}
        <div class="flex flex-wrap gap-x-4 gap-y-2">
          {#each tags as tag (tag.id)}
            <Checkbox checked={v.tag_ids.includes(tag.id)} onchange={(checked) => toggleTag(tag.id, checked)}><TagBadge {tag} /></Checkbox>
          {/each}
        </div>
      {:else}
        <p class="text-xs text-fg-muted">还没有标签，可在节点列表的「标签」中创建。</p>
      {/if}
      {#if form.errors.tag_ids}<p class="text-xs text-danger-text" role="alert">{form.errors.tag_ids}</p>{/if}
    </fieldset>

    <section class="flex flex-col gap-2">
      <div>
        <h3 class="text-[0.8667rem] font-medium">IP 地址</h3>
        <p class="mt-0.5 text-xs text-fg-muted">填写其他节点能连接到的地址。有多个出口或入口 IP 时添加副 IP，可在链路中按连线选用。</p>
      </div>
      <AddressEditor bind:rows={v.rows} network={node?.network} errors={shownRowErrors} bind:advanced error={form.errors.addresses} />
    </section>

    <Disclosure title="更多设置" hint="备注、允许端口、探测{passive ? '与远程唤醒' : '端口'}" bind:open={moreOpen} card={false} class="-mx-4 border-t">
      <div class="grid gap-x-4 gap-y-3 px-4 sm:grid-cols-2">
        <Field label="备注" optional error={form.errors.note} class="sm:col-span-2">
          {#snippet children({ id, invalid })}
            <Textarea {id} {invalid} bind:value={v.note} rows={2} maxlength={500} />
          {/snippet}
        </Field>
        <Field label="允许端口" error={form.errors.ports} hint="转发可使用的入站端口，如 10000-65535 或 10000-10100,20000。">
          {#snippet children({ id, invalid })}
            <Input {id} {invalid} mono bind:value={v.ports} autocomplete="off" />
          {/snippet}
        </Field>
        <Field label="质量探测 UDP 端口" error={form.errors.quality_port} hint="0 为关闭探测。">
          {#snippet children({ id, invalid })}
            <Input {id} {invalid} type="number" bind:value={v.quality_port} step={1} inputmode="numeric" class="tabular" />
          {/snippet}
        </Field>
        {#if passive}
          <Field label="被动节点远程唤醒" error={form.errors.wake_enabled}>
            {#snippet children({ id })}
              <div class="flex h-8 items-center gap-2 text-sm">
                <Switch {id} bind:checked={v.wake_enabled} />
                <span class="text-fg-muted">{v.wake_enabled ? '开启：配置变化时自动唤醒' : '关闭（默认）'}</span>
              </div>
            {/snippet}
          </Field>
          <Field label="唤醒 UDP 端口" error={form.errors.wake_port} hint="与节点本地设置一致，0 为未设置。">
            {#snippet children({ id, invalid })}
              <Input {id} {invalid} type="number" bind:value={v.wake_port} step={1} inputmode="numeric" class="tabular" />
            {/snippet}
          </Field>
          <p class="text-xs leading-relaxed text-fg-muted sm:col-span-2">先在节点运行 <code class="font-mono">sudo ictl</code> 设置白名单及端口。30 秒未确认时等待手动同步。</p>
        {/if}
      </div>
    </Disclosure>
  </div>
</FormDialog>
