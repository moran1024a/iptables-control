<script lang="ts">
  import Maximize from '@lucide/svelte/icons/maximize';
  import Minimize from '@lucide/svelte/icons/minimize';
  import Minus from '@lucide/svelte/icons/minus';
  import Plus from '@lucide/svelte/icons/plus';
  import Tooltip from '$ui/Tooltip.svelte';
  import type Viewport from './Viewport.svelte';

  type Props = { viewport: Viewport | undefined; scale: number; fullscreen: boolean };
  let { viewport, scale, fullscreen = $bindable() }: Props = $props();
  const button = 'flex h-7 min-w-7 items-center justify-center rounded-md px-1.5 text-fg-muted hover:bg-surface-3 hover:text-fg [&_svg]:size-4';
</script>

<div class="flex shrink-0 items-center gap-0.5 rounded-lg border bg-surface p-0.5 shadow-sm" role="group" aria-label="画布缩放">
  <button type="button" class={button} aria-label="缩小" onclick={() => viewport?.zoomBy(1 / 1.2)}><Minus /></button>
  <Tooltip text="恢复 100%">
    <button type="button" class="{button} tabular w-12 text-xs font-medium" aria-label="缩放比例，点击恢复 100%" onclick={() => viewport?.zoomTo(1)}>
      {Math.round(scale * 100)}%
    </button>
  </Tooltip>
  <button type="button" class={button} aria-label="放大" onclick={() => viewport?.zoomBy(1.2)}><Plus /></button>
  <span class="mx-0.5 h-4 w-px bg-border"></span>
  <button type="button" class="{button} px-2 text-xs font-medium" title="缩放到能看到全部卡片" onclick={() => viewport?.fit()}>适应</button>
  <Tooltip text={fullscreen ? '退出全屏 (Esc)' : '全屏'}>
    <button type="button" class={button} aria-label={fullscreen ? '退出全屏' : '全屏'} aria-pressed={fullscreen} onclick={() => (fullscreen = !fullscreen)}>
      {#if fullscreen}<Minimize />{:else}<Maximize />{/if}
    </button>
  </Tooltip>
</div>
