<script lang="ts">
  let { class: klass = 'size-6' }: { class?: string } = $props();
</script>

<svg viewBox="0 0 32 32" class={klass} aria-hidden="true">
  <rect width="32" height="32" rx="8" class="fill-accent" />
  <path d="M8 11h9a4 4 0 0 1 4 4v0a4 4 0 0 0 4 4M8 21h9a4 4 0 0 0 4-4" fill="none" stroke="#fff" stroke-width="2.4" stroke-linecap="round" />
  <circle cx="8" cy="11" r="2.2" fill="#fff" /><circle cx="8" cy="21" r="2.2" fill="#fff" /><circle cx="25" cy="19" r="2.2" fill="#fff" />
</svg>
