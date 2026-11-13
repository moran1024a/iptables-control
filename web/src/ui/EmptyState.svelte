<script lang="ts">
  import type { Component, Snippet } from 'svelte';
  type Props = { icon?: Component<{ class?: string }>; title: string; text?: string; children?: Snippet; class?: string };
  let { icon: Icon, title, text, children, class: klass = '' }: Props = $props();
</script>

<div class="flex flex-col items-center justify-center gap-2 px-6 py-14 text-center {klass}">
  {#if Icon}
    <div class="mb-1 flex size-11 items-center justify-center rounded-xl bg-surface-3 text-fg-muted"><Icon class="size-5" /></div>
  {/if}
  <p class="text-sm font-medium">{title}</p>
  {#if text}<p class="max-w-sm text-[0.8125rem] leading-relaxed text-fg-muted">{text}</p>{/if}
  {#if children}<div class="mt-2 flex flex-wrap justify-center gap-2">{@render children()}</div>{/if}
</div>
