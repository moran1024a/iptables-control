<script lang="ts">
  import { clock } from '$lib/format';
  import type { NodeBar } from '$lib/types';

  type Props = { bar: NodeBar; size?: 'xs' | 'md'; axis?: boolean; class?: string };
  let { bar, size = 'md', axis = false, class: klass = '' }: Props = $props();
  const LABEL = { ok: '正常', fault: '故障', lost: '失联', none: '无数据', white: '无手动同步' } as const;
  const COLOR = {
    ok: 'bg-ok',
    fault: 'bg-warn',
    lost: 'bg-danger',
    none: 'bg-idle/30',
    white: 'bg-surface ring-1 ring-inset ring-border-strong/70',
  } as const;
  const minutes = $derived(Math.round((bar.levels.length * bar.window) / 60));

  function title(index: number) {
    const from = bar.start + index * bar.window;
    let text: string = LABEL[bar.levels[index]] ?? bar.levels[index];
    if ('counts' in bar && bar.counts) {
      const count = bar.counts[index] ?? 0;
      const last = bar.times?.[index];
      text = count ? `手动同步 ${count} 次${last ? `，最近 ${clock(last)}` : ''}；结果见事件记录` : '无手动同步';
    }
    return `${clock(from)} – ${clock(from + bar.window)}  ${text}`;
  }
</script>

<div class={klass}>
  <div class="flex items-stretch gap-px {size === 'xs' ? 'h-1.5' : 'h-5'}" role="img" aria-label="最近 {minutes} 分钟的状态">
    {#each bar.levels as level, index (index)}
      <span
        title={size === 'md' ? title(index) : undefined}
        class="min-w-0 flex-1 {size === 'xs' ? 'rounded-[1px]' : 'rounded-[2px] transition-transform hover:scale-y-125'} {COLOR[level] ?? 'bg-idle/35'}"
      ></span>
    {/each}
  </div>
  {#if axis}
    <div class="mt-1 flex justify-between text-2xs text-fg-subtle"><span>{minutes} 分钟前</span><span>现在</span></div>
  {/if}
</div>
