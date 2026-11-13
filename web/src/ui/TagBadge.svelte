<script lang="ts">
  import type { Tag } from '$lib/types';
  let { tag, class: klass = '' }: { tag: Pick<Tag, 'name' | 'color'>; class?: string } = $props();
  const color = $derived(/^#[0-9a-f]{6}$/i.test(tag.color) ? tag.color : '#3b82f6');
</script>

<span
  title={tag.name}
  style:--tag={color}
  class="inline-flex h-[1.125rem] max-w-28 shrink-0 items-center rounded-sm border border-[color-mix(in_oklab,var(--tag)_35%,transparent)] bg-[color-mix(in_oklab,var(--tag)_12%,transparent)] px-1.5 text-2xs font-medium text-[color-mix(in_oklab,var(--tag)_78%,var(--fg))] {klass}"
>
  <span class="truncate">{tag.name}</span>
</span>
