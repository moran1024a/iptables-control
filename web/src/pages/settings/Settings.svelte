<script lang="ts">
  import { get } from '$lib/api';
  import { usePoll } from '$lib/poll.svelte';
  import { router } from '$lib/router.svelte';
  import { theme, type ThemeChoice } from '$lib/theme.svelte';
  import type { Settings } from '$lib/types';
  import Alert from '$ui/Alert.svelte';
  import Button from '$ui/Button.svelte';
  import Card from '$ui/Card.svelte';
  import Segmented from '$ui/Segmented.svelte';
  import Spinner from '$ui/Spinner.svelte';
  import Page from '../../layout/Page.svelte';
  import AboutCard from './AboutCard.svelte';
  import AccountCard from './AccountCard.svelte';
  import ServerCard from './ServerCard.svelte';

  // Loaded once: nothing here changes on its own, and the cards keep their own unsaved input.
  const settings = usePoll((signal) => get<Settings>('settings', { signal }), { interval: 0 });
  let accountDirty = $state(false);
  let serverDirty = $state(false);

  $effect(() =>
    router.block(() => {
      const cards = [accountDirty && '账号', serverDirty && '服务端'].filter(Boolean);
      return cards.length
        ? { title: '放弃未保存的修改？', body: `「${cards.join('」和「')}」中的修改尚未保存。`, confirmLabel: '放弃修改', cancelLabel: '继续编辑', danger: true }
        : null;
    }),
  );

  const themes: { value: ThemeChoice; label: string }[] = [
    { value: 'auto', label: '跟随系统' },
    { value: 'light', label: '浅色' },
    { value: 'dark', label: '深色' },
  ];
</script>

<Page title="设置">
  <div class="grid items-start gap-4 lg:grid-cols-2">
    <div class="flex min-w-0 flex-col gap-4">
      <AccountCard bind:dirty={accountDirty} />
      <Card title="外观" hint="只保存在当前浏览器">
        <div class="flex flex-wrap items-center justify-between gap-x-4 gap-y-2">
          <div class="min-w-0">
            <p id="theme-label" class="text-[0.8667rem] font-medium">主题</p>
            <p class="text-xs text-fg-muted">
              {theme.choice === 'auto' ? `跟随系统设置，当前为${theme.dark ? '深色' : '浅色'}。` : '选择后立即生效。'}
            </p>
          </div>
          <Segmented value={theme.choice} options={themes} label="主题" onchange={(choice) => theme.set(choice)} />
        </div>
      </Card>
    </div>
    <div class="flex min-w-0 flex-col gap-4">
      {#if settings.data}
        {#if settings.error}<Alert tone="warn">刷新失败：{settings.error}。显示的是上一次的数据。</Alert>{/if}
        <ServerCard settings={settings.data} bind:dirty={serverDirty} onsaved={settings.refresh} />
        <AboutCard settings={settings.data} />
      {:else if settings.error}
        <Alert tone="danger" title="无法读取服务端设置">
          {settings.error}
          {#snippet actions()}<Button size="sm" onclick={settings.refresh}>重试</Button>{/snippet}
        </Alert>
      {:else}
        <div class="flex justify-center rounded-lg border bg-surface py-16 text-fg-subtle"><Spinner class="size-5" /></div>
      {/if}
    </div>
  </div>
</Page>
