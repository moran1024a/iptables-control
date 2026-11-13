<script lang="ts">
  import { session } from '$lib/session.svelte';
  import ConfirmHost from '$ui/ConfirmHost.svelte';
  import Spinner from '$ui/Spinner.svelte';
  import Toaster from '$ui/Toaster.svelte';
  import Shell from './layout/Shell.svelte';
  import Auth from './pages/Auth.svelte';
  import Button from '$ui/Button.svelte';

  void session.load();
</script>

  {#if session.status === 'loading'}
    <div class="flex h-dvh items-center justify-center text-fg-subtle"><Spinner class="size-6" /></div>
  {:else if session.status === 'offline'}
    <div class="flex h-dvh flex-col items-center justify-center gap-3 px-6 text-center">
      <p class="text-sm font-medium">无法连接服务端</p>
      <p class="max-w-sm text-[0.8667rem] text-fg-muted">{session.error}</p>
      <Button onclick={() => session.load()}>重试</Button>
    </div>
  {:else if session.status === 'ready'}
    <Shell />
  {:else}
    <Auth />
  {/if}
  <ConfirmHost />
  <Toaster />
