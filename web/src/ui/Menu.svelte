<script lang="ts" module>
  import type { Component } from 'svelte';
  export type MenuItem =
    | { label: string; icon?: Component<{ class?: string }>; onselect: () => void; danger?: boolean; disabled?: boolean; hint?: string }
    | 'separator';
</script>

<script lang="ts">
  import Ellipsis from '@lucide/svelte/icons/ellipsis';
  import type { Snippet } from 'svelte';
  import { place } from '$lib/floating';

  type Props = { items: MenuItem[]; label?: string; align?: 'start' | 'end'; trigger?: Snippet; class?: string };
  let { items, label = '更多操作', align = 'end', trigger, class: klass = '' }: Props = $props();
  const id = $props.id();
  let button = $state<HTMLButtonElement>();
  let menu = $state<HTMLDivElement>();
  let open = $state(false);

  const entries = () => [...(menu?.querySelectorAll<HTMLButtonElement>('button:not(:disabled)') ?? [])];

  function toggled(event: ToggleEvent) {
    open = event.newState === 'open';
    if (open && button && menu) {
      place(button.getBoundingClientRect(), menu, 'bottom', align, 4);
      entries()[0]?.focus();
    }
  }

  function keydown(event: KeyboardEvent) {
    const list = entries();
    const index = list.indexOf(document.activeElement as HTMLButtonElement);
    const move = { ArrowDown: index + 1, ArrowUp: index - 1, Home: 0, End: list.length - 1 }[event.key];
    if (move === undefined) return;
    event.preventDefault();
    list[(move + list.length) % list.length]?.focus();
  }

  function select(item: Exclude<MenuItem, 'separator'>) {
    menu?.hidePopover();
    button?.focus();
    item.onselect();
  }
</script>

<svelte:window
  onscroll={() => open && menu?.hidePopover()}
  onresize={() => open && menu?.hidePopover()}
/>

<button
  bind:this={button}
  type="button"
  popovertarget="{id}-menu"
  aria-haspopup="menu"
  aria-expanded={open}
  aria-label={trigger ? undefined : label}
  title={trigger ? undefined : label}
  class={trigger
    ? klass
    : `inline-flex size-7 shrink-0 items-center justify-center rounded-md text-fg-muted transition-colors hover:bg-surface-3 hover:text-fg aria-expanded:bg-surface-3 ${klass}`}
>
  {#if trigger}{@render trigger()}{:else}<Ellipsis class="size-4" />{/if}
</button>
<!-- svelte-ignore a11y_no_static_element_interactions, a11y_interactive_supports_focus -->
<div
  bind:this={menu}
  id="{id}-menu"
  popover="auto"
  role="menu"
  ontoggle={toggled}
  onkeydown={keydown}
  class="fixed inset-auto m-0 min-w-40 rounded-lg border bg-surface p-1 text-fg shadow-md open:animate-in"
>
  {#each items as item, index (index)}
    {#if item === 'separator'}
      <div class="-mx-1 my-1 h-px bg-border" role="separator"></div>
    {:else}
      <button
        type="button"
        role="menuitem"
        disabled={item.disabled}
        onclick={() => select(item)}
        class="flex h-8 w-full items-center gap-2 rounded-md px-2 text-left text-sm outline-none select-none disabled:opacity-50 {item.danger
          ? 'text-danger-text hover:bg-danger-soft focus:bg-danger-soft'
          : 'hover:bg-surface-3 focus:bg-surface-3'}"
      >
        {#if item.icon}<item.icon class="size-4 shrink-0 opacity-80" />{/if}
        <span class="flex-1 whitespace-nowrap">{item.label}</span>
        {#if item.hint}<span class="text-xs text-fg-subtle">{item.hint}</span>{/if}
      </button>
    {/if}
  {/each}
</div>
