<script lang="ts">
  type Props = {
    /** One value per slot, oldest first; null = no sample (drawn as a gap, never as zero). */
    values: (number | null)[];
    label: string;
    /** Text for each slot's tooltip. */
    describe?: (index: number) => string;
    class?: string;
  };
  let { values, label, describe, class: klass = '' }: Props = $props();
  const W = 120;
  const H = 28;
  const PAD = 2;
  const known = $derived(values.filter((v): v is number => v !== null));
  // Zero-based scale with headroom, so a quiet machine does not look busy.
  const top = $derived(Math.max(5, ...known) * 1.15);
  const step = $derived(values.length > 1 ? W / (values.length - 1) : W);
  const y = (v: number) => H - PAD - (v / top) * (H - PAD * 2);
  const x = (i: number) => i * step;

  /** Consecutive runs of sampled slots. */
  const runs = $derived.by(() => {
    const out: { i: number; v: number }[][] = [];
    let run: { i: number; v: number }[] = [];
    values.forEach((v, i) => {
      if (v === null) {
        if (run.length) out.push(run);
        run = [];
      } else run.push({ i, v });
    });
    if (run.length) out.push(run);
    return out;
  });
  const line = (run: { i: number; v: number }[]) => run.map((p, n) => `${n ? 'L' : 'M'}${x(p.i).toFixed(1)},${y(p.v).toFixed(1)}`).join('');
  const area = (run: { i: number; v: number }[]) => `${line(run)}L${x(run[run.length - 1].i).toFixed(1)},${H}L${x(run[0].i).toFixed(1)},${H}Z`;
  let hover = $state<number | null>(null);
</script>

{#if known.length}
  <div class="relative text-accent {klass}">
    <svg viewBox="0 0 {W} {H}" preserveAspectRatio="none" class="block h-7 w-full overflow-visible" role="img" aria-label={label}>
      {#each runs as run, index (index)}
        {#if run.length > 1}
          <path d={area(run)} fill="currentColor" opacity="0.12" />
          <path d={line(run)} fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round" stroke-linecap="round" vector-effect="non-scaling-stroke" />
        {:else}
          <line x1={x(run[0].i) - 0.01} x2={x(run[0].i) + 0.01} y1={y(run[0].v)} y2={y(run[0].v)} stroke="currentColor" stroke-width="4" stroke-linecap="round" vector-effect="non-scaling-stroke" />
        {/if}
      {/each}
      {#if hover !== null && values[hover] !== null}
        <line x1={x(hover)} x2={x(hover)} y1="0" y2={H} stroke="currentColor" stroke-width="1" opacity="0.5" vector-effect="non-scaling-stroke" />
      {/if}
    </svg>
    {#if describe}
      <!-- Hover targets: one full-height strip per slot. -->
      <div class="absolute inset-0 flex" aria-hidden="true">
        {#each values as _, index (index)}
          <span class="h-full flex-1" title={describe(index)} onpointerenter={() => (hover = index)} onpointerleave={() => (hover = null)} role="presentation"></span>
        {/each}
      </div>
    {/if}
  </div>
{/if}
