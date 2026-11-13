<script lang="ts">
  import Plus from '@lucide/svelte/icons/plus';
  import X from '@lucide/svelte/icons/x';
  import { untrack } from 'svelte';
  import { patch, post } from '$lib/api';
  import { FormState, snapshot } from '$lib/form.svelte';
  import { toast } from '$lib/toast.svelte';
  import type { Group, Target } from '$lib/types';
  import Alert from '$ui/Alert.svelte';
  import Button from '$ui/Button.svelte';
  import Field from '$ui/Field.svelte';
  import FormDialog from '$ui/FormDialog.svelte';
  import Input from '$ui/Input.svelte';
  import Segmented from '$ui/Segmented.svelte';
  import Select from '$ui/Select.svelte';
  import Textarea from '$ui/Textarea.svelte';
  import {
    buildSave,
    checkAddresses,
    draftFor,
    emptyRow,
    isImported,
    MAX_ADDRESSES,
    parsePort,
    typeName,
    type TargetDraft,
    type TargetMode,
  } from './target';

  type Props = {
    open?: boolean;
    /** Edit this target; omit to add one. */
    target?: Target | null;
    groups: Group[];
    onsaved: () => void;
  };
  let { open = $bindable(false), target = null, groups, onsaved }: Props = $props();

  const form = new FormState(['name', 'group_id', 'host', 'port', 'uri', 'addresses']);
  let mode = $state<TargetMode>('manual');
  let draft = $state<TargetDraft>(draftFor(null, ''));
  let initial = $state('');
  /** Set after a submit found a bad address row: the rows then show their problems live. */
  let marking = $state(false);
  let rowsEl = $state<HTMLDivElement>();

  // Fill the form each time the dialog opens; later list refreshes must not overwrite what is being typed.
  $effect(() => {
    if (!open) return;
    untrack(() => {
      mode = target && isImported(target) ? 'link' : 'manual';
      draft = draftFor(target, String(groups.find((group) => group.builtin)?.id ?? groups[0]?.id ?? ''));
      initial = snapshot(draft);
      marking = false;
      form.clear();
    });
  });

  const dirty = $derived(snapshot(draft) !== initial);
  const showGroup = $derived(groups.length > 1);
  const main = $derived(
    mode === 'manual'
      ? { host: draft.host.trim(), port: parsePort(draft.port) }
      : target && draft.uri.trim() === target.uri
        ? { host: target.host, port: target.port }
        : null,
  );
  const problems = $derived(marking ? checkAddresses(draft, main) : {});
  // Server errors naming a field this mode does not render still have to be visible.
  const orphanError = $derived(
    (mode === 'manual' ? form.errors.uri : form.errors.host || form.errors.port) || (!showGroup && form.errors.group_id) || '',
  );

  function addRow() {
    draft.addresses.push(emptyRow());
    requestAnimationFrame(() => rowsEl?.querySelector<HTMLInputElement>('[data-row]:last-child input')?.focus());
  }

  async function submit() {
    const ok = await form.run(async () => {
      marking = true;
      const body = buildSave(draft, mode, target);
      if (target) await patch(`targets/${target.id}`, body);
      else await post('targets', body);
    });
    if (!ok) return;
    open = false;
    toast.ok(target ? '已保存' : '已保存，可在链路中选择此目标');
    onsaved();
  }
</script>

<FormDialog bind:open title={target ? '编辑目标' : '添加目标'} size="lg" {form} {dirty} onsubmit={submit}>
  {#if !target}
    <Segmented
      bind:value={mode}
      label="添加方式"
      class="self-start"
      options={[
        { value: 'manual', label: '手动填写' },
        { value: 'link', label: '导入链接' },
      ]}
    />
  {/if}

  <div class="grid gap-4 {showGroup ? 'sm:grid-cols-[minmax(0,1fr)_12rem]' : ''}">
    <Field
      label="名称"
      optional={!target}
      error={form.errors.name}
      hint={target ? undefined : mode === 'manual' ? '留空时使用地址作为名称。' : '留空时使用链接自带的名称。'}
    >
      {#snippet children({ id, invalid })}
        <Input {id} {invalid} bind:value={draft.name} maxlength={64} autocomplete="off" />
      {/snippet}
    </Field>
    {#if showGroup}
      <Field label="分组" error={form.errors.group_id}>
        {#snippet children({ id, invalid })}
          <Select {id} {invalid} bind:value={draft.group} options={groups.map((group) => ({ value: String(group.id), label: group.name }))} />
        {/snippet}
      </Field>
    {/if}
  </div>

  {#if mode === 'manual'}
    <div class="grid grid-cols-[minmax(0,1fr)_6.5rem] gap-3">
      <Field label="地址" error={form.errors.host} hint="IPv4 或域名">
        {#snippet children({ id, invalid })}
          <Input {id} {invalid} mono bind:value={draft.host} placeholder="203.0.113.10" autocomplete="off" spellcheck={false} autocapitalize="off" />
        {/snippet}
      </Field>
      <Field label="端口" error={form.errors.port} hint="1-65535">
        {#snippet children({ id, invalid })}
          <Input {id} {invalid} mono bind:value={draft.port} inputmode="numeric" maxlength={5} placeholder="443" autocomplete="off" />
        {/snippet}
      </Field>
    </div>
  {:else}
    <Field
      label="分享链接"
      error={form.errors.uri}
      hint={target
        ? `当前为 ${typeName(target)} · ${target.host}:${target.port}。连接参数只能通过粘贴新的分享链接修改。`
        : '支持 SS、SS2022 和 VLESS ENC，服务器地址需为 IPv4 或域名。'}
    >
      {#snippet children({ id, invalid })}
        <Textarea
          {id}
          {invalid}
          mono
          rows={4}
          bind:value={draft.uri}
          maxlength={8192}
          spellcheck={false}
          autocapitalize="off"
          class="break-all"
          placeholder="ss://… 或 vless://…?encryption=mlkem768x25519plus…"
        />
      {/snippet}
    </Field>
  {/if}

  <fieldset class="flex min-w-0 flex-col gap-2">
    <div class="flex items-baseline justify-between gap-2">
      <legend class="float-left text-[0.8125rem] font-medium">其他接入地址</legend>
      <span class="text-xs text-fg-subtle">{draft.addresses.length} / {MAX_ADDRESSES}</span>
    </div>
    <p class="text-xs leading-relaxed text-fg-muted">同一目标的备用 IP 或端口，在链路画布的连线上可以选择具体走哪一个。</p>
    {#if draft.addresses.length}
      <div class="grid-cols-[minmax(0,0.8fr)_minmax(0,1.2fr)_5.5rem_1.75rem] gap-2 px-0 text-xs text-fg-muted max-sm:hidden sm:grid" aria-hidden="true">
        <span>名称（可选）</span><span>IPv4 或域名</span><span>端口</span>
      </div>
      <div bind:this={rowsEl} class="flex flex-col gap-2">
        {#each draft.addresses as row, index (row.key)}
          {@const problem = problems[row.key]}
          <div
            data-row
            class="grid grid-cols-[minmax(0,1fr)_5.5rem_1.75rem] items-center gap-2 max-sm:rounded-md max-sm:border max-sm:bg-surface-2/60 max-sm:p-2 sm:grid-cols-[minmax(0,0.8fr)_minmax(0,1.2fr)_5.5rem_1.75rem]"
          >
            <Input bind:value={row.name} class="max-sm:col-span-3" maxlength={64} placeholder="名称（可选）" aria-label="第 {index + 1} 个接入地址的名称" autocomplete="off" />
            <Input
              bind:value={row.host}
              mono
              invalid={!!problem?.host}
              title={problem?.host}
              placeholder="IPv4 或域名"
              aria-label="第 {index + 1} 个接入地址的 IP 或域名"
              autocomplete="off"
              spellcheck={false}
              autocapitalize="off"
            />
            <Input
              bind:value={row.port}
              mono
              invalid={!!problem?.port}
              title={problem?.port}
              inputmode="numeric"
              maxlength={5}
              placeholder="端口"
              aria-label="第 {index + 1} 个接入地址的端口"
              autocomplete="off"
            />
            <Button variant="ghost" size="sm" icon aria-label="移除第 {index + 1} 个接入地址" title="移除" onclick={() => draft.addresses.splice(index, 1)}>
              <X />
            </Button>
          </div>
        {/each}
      </div>
    {/if}
    {#if form.errors.addresses}<p class="text-xs text-danger-text" role="alert">{form.errors.addresses}</p>{/if}
    <Button size="sm" class="self-start" onclick={addRow} disabled={draft.addresses.length >= MAX_ADDRESSES}><Plus />添加接入地址</Button>
  </fieldset>

  {#if target?.configs.length}
    <Alert tone="info">此目标正被 {target.configs.length} 个配置使用，修改地址后这些配置会重新同步。</Alert>
  {/if}
  {#if orphanError}<Alert tone="danger">{orphanError}</Alert>{/if}
</FormDialog>
