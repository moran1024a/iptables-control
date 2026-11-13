<script lang="ts">
  import Copy from '@lucide/svelte/icons/copy';
  import Pencil from '@lucide/svelte/icons/pencil';
  import Trash2 from '@lucide/svelte/icons/trash-2';
  import { copyText } from '$lib/clipboard';
  import type { Target } from '$lib/types';
  import Button from '$ui/Button.svelte';
  import Menu from '$ui/Menu.svelte';
  import Spinner from '$ui/Spinner.svelte';
  import { copyValue, isImported, typeLabel, typeName } from './target';

  type Props = { target: Target; busy?: boolean; onedit: (target: Target) => void; ondelete: (target: Target) => void };
  let { target, busy = false, onedit, ondelete }: Props = $props();
  const extra = $derived(target.addresses.filter((address) => address.id !== 'main'));
  const imported = $derived(isImported(target));
  const usage = $derived(target.configs.map((config) => config.remark).join('、'));
</script>

<div
  class="grid grid-cols-[minmax(0,1fr)_auto] items-center gap-x-4 px-4 py-2.5 transition-colors hover:bg-surface-2 md:grid-cols-[minmax(0,1fr)_minmax(0,18rem)_auto] {busy
    ? 'opacity-60'
    : ''}"
>
  <div class="min-w-0">
    <div class="flex min-w-0 items-center gap-2">
      <span
        class="shrink-0 rounded-sm border px-1 text-2xs leading-4 font-medium {imported ? 'border-accent/30 text-accent-text' : 'text-fg-muted'}"
        title={typeName(target)}
      >
        {typeLabel(target)}
      </span>
      <button
        type="button"
        class="min-w-0 truncate rounded-sm text-left text-sm font-medium hover:text-accent-text"
        title={target.name}
        onclick={() => onedit(target)}
      >
        {target.name}
      </button>
    </div>
    <div class="mt-0.5 flex min-w-0 items-center gap-1.5 text-xs text-fg-muted">
      <code class="truncate" title="{target.host}:{target.port}">{target.host}:{target.port}</code>
      {#if extra.length}
        <span
          class="shrink-0 rounded-sm bg-surface-3 px-1 text-2xs"
          title={extra.map((address) => `${address.name ? address.name + ' ' : ''}${address.host}:${address.port}`).join('\n')}
        >
          ＋{extra.length} 地址
        </span>
      {/if}
      <span class="shrink-0 md:hidden" title={usage || undefined}>· {target.configs.length ? `${target.configs.length} 个配置在用` : '未使用'}</span>
    </div>
  </div>
  <div class="min-w-0 text-xs max-md:hidden">
    {#if target.configs.length}
      <p class="truncate text-fg-muted" title={usage}><span class="text-fg-subtle">用于</span> {usage}</p>
    {:else}
      <p class="text-fg-subtle">未使用</p>
    {/if}
  </div>
  <div class="flex items-center gap-0.5">
    {#if busy}<Spinner class="mr-1 size-4 text-fg-subtle" />{/if}
    <Button variant="ghost" size="sm" icon aria-label="编辑 {target.name}" title="编辑" disabled={busy} onclick={() => onedit(target)}><Pencil /></Button>
    <Menu
      label="{target.name} 的更多操作"
      items={[
        { label: imported ? '复制分享链接' : '复制地址', icon: Copy, onselect: () => void copyText(copyValue(target)) },
        'separator',
        { label: '删除', icon: Trash2, danger: true, disabled: busy, onselect: () => ondelete(target) },
      ]}
    />
  </div>
</div>
