<script lang="ts">
  import type { Tag } from '$lib/types';
  import TagBadge from './TagBadge.svelte';

  type Props = { tags: Pick<Tag, 'id' | 'name' | 'color'>[]; limit?: number; expandable?: boolean; class?: string };
  let { tags, limit = 2, expandable = true, class: klass = '' }: Props = $props();
  let expanded = $state(false);
  const hidden = $derived(tags.slice(limit));
  const shown = $derived(expanded ? tags : tags.slice(0, limit));
</script>

{#if tags.length}
  <span class="inline-flex min-w-0 flex-wrap items-center gap-1 {klass}">
    {#each shown as tag (tag.id)}<TagBadge {tag} />{/each}
    {#if hidden.length}
      {#if expandable}
        <button
          type="button"
          aria-expanded={expanded}
          title={expanded ? undefined : hidden.map((tag) => tag.name).join('、')}
          onclick={(event) => {
            event.preventDefault();
            event.stopPropagation();
            expanded = !expanded;
          }}
          class="inline-flex h-[1.125rem] items-center rounded-sm bg-surface-3 px-1.5 text-2xs font-medium text-fg-muted hover:text-fg"
        >
          {expanded ? '收起' : `+${hidden.length}`}
        </button>
      {:else}
        <span title={hidden.map((tag) => tag.name).join('、')} class="inline-flex h-[1.125rem] items-center rounded-sm bg-surface-3 px-1.5 text-2xs font-medium text-fg-muted">
          +{hidden.length}
        </span>
      {/if}
    {/if}
  </span>
{/if}
