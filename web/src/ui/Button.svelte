<script lang="ts">
  import type { Snippet } from 'svelte';
  import type { HTMLAnchorAttributes, HTMLButtonAttributes } from 'svelte/elements';
  import Spinner from './Spinner.svelte';

  type Variant = 'primary' | 'secondary' | 'ghost' | 'danger' | 'danger-ghost';
  type Size = 'xs' | 'sm' | 'md';
  type Props = {
    variant?: Variant;
    size?: Size;
    loading?: boolean;
    /** Square button for a lone icon; pass `aria-label`. */
    icon?: boolean;
    href?: string;
    children?: Snippet;
  } & Omit<HTMLButtonAttributes, 'children'> &
    Pick<HTMLAnchorAttributes, 'target' | 'rel' | 'download'>;

  let {
    variant = 'secondary',
    size = 'md',
    loading = false,
    icon = false,
    href,
    children,
    class: klass = '',
    disabled,
    type = 'button',
    ...rest
  }: Props = $props();

  const variants: Record<Variant, string> = {
    primary: 'bg-accent text-accent-fg hover:bg-accent-hover shadow-sm',
    secondary: 'bg-surface text-fg border border-border-strong/70 hover:bg-surface-2 shadow-sm',
    ghost: 'text-fg-muted hover:bg-surface-3 hover:text-fg',
    danger: 'bg-danger text-white hover:bg-danger-hover shadow-sm',
    'danger-ghost': 'text-danger-text hover:bg-danger-soft',
  };
  const sizes: Record<Size, string> = {
    xs: 'h-6 text-xs gap-1 rounded-sm',
    sm: 'h-7 text-[0.8667rem] gap-1.5 rounded-md',
    md: 'h-8 text-sm gap-1.5 rounded-md',
  };
  const pads: Record<Size, [string, string]> = { xs: ['px-2', 'w-6'], sm: ['px-2.5', 'w-7'], md: ['px-3', 'w-8'] };
  const classes = $derived(
    `inline-flex shrink-0 items-center justify-center font-medium whitespace-nowrap select-none transition-colors duration-100 disabled:opacity-50 [&_svg]:size-[1.05em] [&_svg]:shrink-0 ${variants[variant]} ${sizes[size]} ${pads[size][icon ? 1 : 0]} ${klass}`,
  );
</script>

{#if href && !disabled}
  <a {href} class={classes} {...rest as HTMLAnchorAttributes}>{@render children?.()}</a>
{:else}
  <button {type} class={classes} disabled={disabled || loading} aria-busy={loading || undefined} {...rest}>
    {#if loading}<Spinner />{/if}
    {@render children?.()}
  </button>
{/if}
