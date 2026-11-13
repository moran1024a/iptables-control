<script lang="ts">
  import CircleAlert from '@lucide/svelte/icons/circle-alert';
  import Info from '@lucide/svelte/icons/info';
  import TriangleAlert from '@lucide/svelte/icons/triangle-alert';
  import type { Snippet } from 'svelte';

  type Props = { tone?: 'info' | 'warn' | 'danger'; title?: string; children?: Snippet; actions?: Snippet; class?: string };
  let { tone = 'info', title, children, actions, class: klass = '' }: Props = $props();
  const tones = {
    info: 'border-info/25 bg-info-soft text-info-text',
    warn: 'border-warn/30 bg-warn-soft text-warn-text',
    danger: 'border-danger/25 bg-danger-soft text-danger-text',
  };
</script>

<div class="flex items-start gap-2.5 rounded-lg border px-3 py-2.5 text-[0.8667rem] leading-relaxed {tones[tone]} {klass}" role={tone === 'danger' ? 'alert' : 'note'}>
  {#if tone === 'info'}<Info class="mt-0.5 size-4 shrink-0" />
  {:else if tone === 'warn'}<TriangleAlert class="mt-0.5 size-4 shrink-0" />
  {:else}<CircleAlert class="mt-0.5 size-4 shrink-0" />{/if}
  <div class="min-w-0 flex-1">
    {#if title}<p class="font-medium">{title}</p>{/if}
    {#if children}<div class="break-words {title ? 'mt-0.5 opacity-90' : ''}">{@render children()}</div>{/if}
  </div>
  {#if actions}<div class="flex shrink-0 gap-1.5">{@render actions()}</div>{/if}
</div>
