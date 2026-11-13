<script lang="ts">
  import ChevronDown from '@lucide/svelte/icons/chevron-down';
  import Plus from '@lucide/svelte/icons/plus';
  import Trash2 from '@lucide/svelte/icons/trash-2';
  import { tick } from 'svelte';
  import type { NodeDetail } from '$lib/types';
  import Button from '$ui/Button.svelte';
  import Field from '$ui/Field.svelte';
  import Input from '$ui/Input.svelte';
  import { autofillDevices, emptyRow, MAX_SECONDARY, type AddressRow, type RowErrors } from './node-logic';

  type Props = {
    rows: AddressRow[];
    /** IPv4 addresses and NICs the agent found on the machine; offered as suggestions. */
    network?: NodeDetail['node']['network'] | null;
    /** Client-side errors per row id. */
    errors?: Record<string, RowErrors>;
    /** Row ids whose advanced section is open. */
    advanced?: Record<string, boolean>;
    /** Error for the list as a whole (server `addresses`). */
    error?: string;
  };
  let { rows = $bindable(), network = null, errors = {}, advanced = $bindable({}), error = '' }: Props = $props();
  const uid = $props.id();
  const locals = $derived([...new Set((network?.addresses ?? []).map((a) => a.local).filter(Boolean))]);
  const devices = $derived([...new Set((network?.addresses ?? []).map((a) => a.device).filter(Boolean))]);
  const full = $derived(rows.filter((row) => !row.primary).length >= MAX_SECONDARY);
  const advancedSet = (row: AddressRow) => [row.local, row.device, row.gateway].filter((v) => v.trim()).length;

  /** A new address invalidates what was derived from the old one. */
  function hostChanged(row: AddressRow) {
    if (!row.local.trim()) row.device = '';
    rows = autofillDevices(rows, network);
  }
  function localChanged(row: AddressRow) {
    row.device = '';
    rows = autofillDevices(rows, network);
  }
  function makePrimary(row: AddressRow) {
    for (const other of rows) other.primary = other === row;
  }
  async function add() {
    if (full) return;
    const row = emptyRow();
    rows.push(row);
    await tick();
    document.querySelector<HTMLInputElement>(`[data-address-host="${row.id}"]`)?.focus();
  }
  function remove(row: AddressRow) {
    if (!row.primary) rows = rows.filter((other) => other !== row);
  }
</script>

<datalist id="{uid}-locals">{#each locals as value (value)}<option {value}></option>{/each}</datalist>
<datalist id="{uid}-devices">{#each devices as value (value)}<option {value}></option>{/each}</datalist>

<div class="flex flex-col gap-2">
  {#each rows as row, index (row.id)}
    {@const rowErrors = errors[row.id] ?? {}}
    {@const open = !!advanced[row.id]}
    <fieldset class="rounded-lg border bg-surface-2/40 p-3 {row.primary ? 'border-accent/40' : ''}">
      <legend class="sr-only">{row.primary ? '主 IP' : `副 IP ${index}`}</legend>
      <div class="mb-2 flex items-center gap-2">
        {#if row.primary}
          <span class="rounded-sm bg-accent-soft px-1.5 text-xs leading-5 font-medium text-accent-text">主 IP</span>
          <span class="truncate text-xs text-fg-muted">其他节点默认连接此地址，可填域名</span>
        {:else}
          <span class="rounded-sm bg-surface-3 px-1.5 text-xs leading-5 font-medium text-fg-muted">副 IP</span>
          <Button size="xs" variant="ghost" onclick={() => makePrimary(row)}>设为主 IP</Button>
        {/if}
        <Button
          size="sm"
          variant="danger-ghost"
          icon
          class="-my-1 ml-auto"
          disabled={row.primary}
          aria-label="删除地址"
          title={row.primary ? '主 IP 不能删除；先把另一个地址设为主 IP' : '删除地址'}
          onclick={() => remove(row)}
        >
          <Trash2 />
        </Button>
      </div>
      <div class="grid gap-x-3 gap-y-2 sm:grid-cols-2">
        <Field label={row.primary ? 'IP 地址或域名' : 'IP 地址'} error={rowErrors.host}>
          {#snippet children({ id, invalid })}
            <Input
              {id}
              {invalid}
              mono
              bind:value={row.host}
              onchange={() => hostChanged(row)}
              list="{uid}-locals"
              placeholder={row.primary ? '203.0.113.10' : '203.0.113.11'}
              autocomplete="off"
              autocapitalize="off"
              spellcheck={false}
              data-address-host={row.id}
            />
          {/snippet}
        </Field>
        <Field label="名称" optional error={rowErrors.name}>
          {#snippet children({ id, invalid })}
            <Input {id} {invalid} bind:value={row.name} maxlength={64} placeholder="如：电信线路" autocomplete="off" />
          {/snippet}
        </Field>
      </div>
      <button
        type="button"
        class="mt-2 -mb-1 inline-flex items-center gap-1 rounded-sm py-1 text-xs font-medium text-fg-muted hover:text-fg"
        aria-expanded={open}
        aria-controls="{uid}-{row.id}-adv"
        onclick={() => (advanced = { ...advanced, [row.id]: !open })}
      >
        <ChevronDown class="size-3.5 transition-transform {open ? '' : '-rotate-90'}" />高级设置
        {#if !open && advancedSet(row)}<span class="font-normal text-fg-subtle">· 已设置 {advancedSet(row)} 项</span>{/if}
      </button>
      {#if open}
        <div id="{uid}-{row.id}-adv" class="mt-2 flex flex-col gap-2">
          <p class="text-xs leading-relaxed text-fg-muted">仅用于指定出口；自动出口无需设置。公网出口由服务商映射时，这里填写本机源 IP。</p>
          <div class="grid gap-x-3 gap-y-2 sm:grid-cols-3">
            <Field label="本机源 IP" error={rowErrors.local}>
              {#snippet children({ id, invalid })}
                <Input {id} {invalid} mono bind:value={row.local} onchange={() => localChanged(row)} list="{uid}-locals" placeholder="默认使用此 IP" autocomplete="off" />
              {/snippet}
            </Field>
            <Field label="网卡" error={rowErrors.device}>
              {#snippet children({ id, invalid })}
                <Input {id} {invalid} mono bind:value={row.device} list="{uid}-devices" placeholder="自动识别" maxlength={15} autocomplete="off" autocapitalize="off" />
              {/snippet}
            </Field>
            <Field label="网关" error={rowErrors.gateway}>
              {#snippet children({ id, invalid })}
                <Input {id} {invalid} mono bind:value={row.gateway} placeholder="自动识别" autocomplete="off" />
              {/snippet}
            </Field>
          </div>
        </div>
      {/if}
    </fieldset>
  {/each}
  {#if error}<p class="text-xs text-danger-text" role="alert">{error}</p>{/if}
  <div class="flex flex-wrap items-center gap-2">
    <Button size="sm" onclick={add} disabled={full}><Plus />添加副 IP</Button>
    {#if full}<span class="text-xs text-fg-muted">最多添加 {MAX_SECONDARY} 个副 IP</span>{/if}
  </div>
</div>
