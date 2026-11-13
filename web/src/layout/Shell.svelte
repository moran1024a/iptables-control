<script lang="ts">
  import FileKey from '@lucide/svelte/icons/file-key-2';
  import LogOut from '@lucide/svelte/icons/log-out';
  import Moon from '@lucide/svelte/icons/moon';
  import Rss from '@lucide/svelte/icons/rss';
  import Server from '@lucide/svelte/icons/server';
  import Settings from '@lucide/svelte/icons/settings';
  import Sun from '@lucide/svelte/icons/sun';
  import Crosshair from '@lucide/svelte/icons/crosshair';
  import Waypoints from '@lucide/svelte/icons/waypoints';
  import type { Component } from 'svelte';
  import { href, router } from '$lib/router.svelte';
  import { session } from '$lib/session.svelte';
  import { theme } from '$lib/theme.svelte';
  import Button from '$ui/Button.svelte';
  import Tooltip from '$ui/Tooltip.svelte';
  import Logo from './Logo.svelte';
  import Spinner from '$ui/Spinner.svelte';
  import Nodes from '../pages/nodes/Nodes.svelte';

  // The landing page ships in the main bundle; every other page loads on first visit.
  type Loader = () => Promise<{ default: Component }>;
  type Entry = { page: string; label: string; icon: Component<{ class?: string }>; load: Loader };
  const pages: Entry[] = [
    { page: 'nodes', label: '节点', icon: Server, load: async () => ({ default: Nodes }) },
    { page: 'targets', label: '目标', icon: Crosshair, load: () => import('../pages/targets/Targets.svelte') },
    { page: 'routes', label: '链路', icon: Waypoints, load: () => import('../pages/links/Links.svelte') },
    { page: 'configs', label: '配置', icon: FileKey, load: () => import('../pages/configs/Configs.svelte') },
    { page: 'subscriptions', label: '订阅', icon: Rss, load: () => import('../pages/subscriptions/Subscriptions.svelte') },
    { page: 'settings', label: '设置', icon: Settings, load: () => import('../pages/settings/Settings.svelte') },
  ];
  const current = $derived(pages.find((entry) => entry.page === router.page));
  let failed = $state('');

  // Warm the cache when the pointer reaches a navigation item, so the switch feels instant.
  const warm = (entry: Entry) => void entry.load().catch(() => {});

  $effect(() => {
    // Old bookmarks: #/quality/topologies/<id> moved under the link itself.
    if (router.page === 'quality' && router.parts[1] === 'topologies' && router.id(2)) router.go(`routes/${router.id(2)}/quality`, { replace: true });
    else if (!current) router.go('nodes', { replace: true });
  });
  $effect(() => {
    document.title = current ? `${current.label} · ictl` : 'ictl';
  });
</script>

<div class="flex min-h-dvh">
  <aside class="sticky top-0 hidden h-dvh w-52 shrink-0 flex-col border-r bg-surface lg:flex">
    <a href={href('nodes')} class="flex h-14 items-center gap-2.5 px-4">
      <Logo class="size-7" />
      <span class="text-[0.9375rem] font-semibold tracking-tight">ictl</span>
    </a>
    <nav class="flex flex-1 flex-col gap-0.5 px-2.5 py-1" aria-label="主导航">
      {#each pages as entry (entry.page)}
        {@const active = entry.page === router.page}
        <a
          href={href(entry.page)}
          aria-current={active ? 'page' : undefined}
          onpointerenter={() => warm(entry)}
          onfocus={() => warm(entry)}
          class="flex h-8 items-center gap-2.5 rounded-md px-2.5 text-sm font-medium transition-colors {active
            ? 'bg-accent-soft text-accent-text'
            : 'text-fg-muted hover:bg-surface-3 hover:text-fg'}"
        >
          <entry.icon class="size-4 shrink-0" />
          {entry.label}
        </a>
      {/each}
    </nav>
    <div class="flex items-center gap-1 border-t px-2.5 py-2.5">
      <div class="min-w-0 flex-1 px-1.5">
        <p class="truncate text-[0.8125rem] font-medium">{session.username}</p>
        <p class="truncate text-2xs text-fg-subtle">v{session.version}</p>
      </div>
      <Tooltip text={theme.dark ? '切换到浅色' : '切换到深色'}>
        <button
          type="button"
          class="flex size-7 items-center justify-center rounded-md text-fg-muted hover:bg-surface-3 hover:text-fg"
          aria-label="切换主题"
          onclick={() => theme.set(theme.dark ? 'light' : 'dark')}
        >
          {#if theme.dark}<Sun class="size-4" />{:else}<Moon class="size-4" />{/if}
        </button>
      </Tooltip>
      <Tooltip text="退出登录">
        <button
          type="button"
          class="flex size-7 items-center justify-center rounded-md text-fg-muted hover:bg-surface-3 hover:text-fg"
          aria-label="退出登录"
          onclick={() => session.logout()}
        >
          <LogOut class="size-4" />
        </button>
      </Tooltip>
    </div>
  </aside>

  <div class="flex min-w-0 flex-1 flex-col pb-14 lg:pb-0">
    <header class="sticky top-0 z-30 flex h-12 items-center gap-2.5 border-b bg-surface/90 px-4 backdrop-blur lg:hidden">
      <Logo class="size-6" />
      <span class="flex-1 text-[0.9375rem] font-semibold">{current?.label ?? 'ictl'}</span>
      <button type="button" class="flex size-8 items-center justify-center rounded-md text-fg-muted" aria-label="切换主题" onclick={() => theme.set(theme.dark ? 'light' : 'dark')}>
        {#if theme.dark}<Sun class="size-4" />{:else}<Moon class="size-4" />{/if}
      </button>
      <button type="button" class="flex size-8 items-center justify-center rounded-md text-fg-muted" aria-label="退出登录" onclick={() => session.logout()}>
        <LogOut class="size-4" />
      </button>
    </header>
    <main class="min-w-0 flex-1">
      {#if current}
        {#key current.page + failed}
          {#await current.load()}
            <div class="flex justify-center py-24 text-fg-subtle"><Spinner class="size-5" /></div>
          {:then module}
            <module.default />
          {:catch}
            <div class="flex flex-col items-center gap-3 px-6 py-24 text-center">
              <p class="text-sm font-medium">页面加载失败</p>
              <p class="max-w-sm text-[0.8125rem] text-fg-muted">可能是网络中断，或面板刚刚升级。重试无效时请刷新页面。</p>
              <div class="flex gap-2">
                <Button onclick={() => (failed = String(Date.now()))}>重试</Button>
                <Button variant="primary" onclick={() => location.reload()}>刷新页面</Button>
              </div>
            </div>
          {/await}
        {/key}
      {/if}
    </main>
  </div>

  <nav class="fixed inset-x-0 bottom-0 z-30 flex h-14 items-stretch border-t bg-surface/95 backdrop-blur lg:hidden" aria-label="主导航">
    {#each pages as entry (entry.page)}
      {@const active = entry.page === router.page}
      <a
        href={href(entry.page)}
        aria-current={active ? 'page' : undefined}
        class="flex flex-1 flex-col items-center justify-center gap-0.5 text-2xs font-medium {active ? 'text-accent-text' : 'text-fg-muted'}"
      >
        <entry.icon class="size-[1.15rem]" />
        {entry.label}
      </a>
    {/each}
  </nav>
</div>
