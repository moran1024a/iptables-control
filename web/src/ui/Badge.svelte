<script lang="ts" module>
  export type Tone = 'ok' | 'warn' | 'danger' | 'info' | 'idle' | 'accent';
</script>

<script lang="ts">
  import type { Snippet } from 'svelte';
  type Props = { tone?: Tone; dot?: boolean; pulse?: boolean; children: Snippet; class?: string; title?: string };
  let { tone = 'idle', dot = false, pulse = false, children, class: klass = '', title }: Props = $props();
  const tones: Record<Tone, string> = {
    ok: 'bg-ok-soft text-ok-text',
    warn: 'bg-warn-soft text-warn-text',
    danger: 'bg-danger-soft text-danger-text',
    info: 'bg-info-soft text-info-text',
    idle: 'bg-idle-soft text-fg-muted',
    accent: 'bg-accent-soft text-accent-text',
  };
  const dots: Record<Tone, string> = {
    ok: 'bg-ok',
    warn: 'bg-warn',
    danger: 'bg-danger',
    info: 'bg-info',
    idle: 'bg-idle',
    accent: 'bg-accent',
  };
</script>

<span
  {title}
  class="inline-flex h-5 max-w-full shrink-0 items-center gap-1.5 rounded-full px-2 text-xs font-medium whitespace-nowrap {tones[tone]} {klass}"
>
  {#if dot}
    <span class="relative flex size-1.5">
      {#if pulse}<span class="absolute inline-flex size-full animate-ping rounded-full opacity-60 {dots[tone]}"></span>{/if}
      <span class="relative inline-flex size-1.5 rounded-full {dots[tone]}"></span>
    </span>
  {/if}
  <span class="truncate">{@render children()}</span>
</span>
