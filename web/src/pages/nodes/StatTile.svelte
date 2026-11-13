<script lang="ts">
  import type { Snippet } from 'svelte';

  type Props = {
    label: string;
    value: string;
    /** Second line, e.g. "可用 1.2 GiB / 共 2 GiB". */
    detail?: string;
    /** 0–100 fill of the usage meter; omit for no meter. */
    percent?: number | null;
    children?: Snippet;
  };
  let { label, value, detail, percent, children }: Props = $props();
  // Status colour only when it means something: nearly full is a warning, full is critical.
  const tone = $derived(percent == null ? '' : percent >= 95 ? 'bg-danger' : percent >= 85 ? 'bg-warn' : 'bg-accent');
</script>

<div class="flex min-w-0 flex-col gap-1.5 rounded-lg border bg-surface-2/50 p-3">
  <div class="text-xs text-fg-muted">{label}</div>
  <div class="tabular text-lg leading-6 font-semibold">{value}</div>
  {#if percent != null}
    <div class="h-1 overflow-hidden rounded-full bg-surface-3" role="meter" aria-label={label} aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(percent)}>
      <div class="h-full rounded-full {tone}" style:width="{Math.max(percent, percent > 0 ? 2 : 0)}%"></div>
    </div>
  {/if}
  {#if detail}<div class="tabular text-xs leading-snug text-fg-muted">{detail}</div>{/if}
  {@render children?.()}
</div>
