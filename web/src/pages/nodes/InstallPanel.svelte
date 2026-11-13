<script lang="ts">
  import Eye from '@lucide/svelte/icons/eye';
  import EyeOff from '@lucide/svelte/icons/eye-off';
  import Terminal from '@lucide/svelte/icons/terminal';
  import type { NodeDetail } from '$lib/types';
  import Button from '$ui/Button.svelte';
  import Card from '$ui/Card.svelte';
  import CopyButton from '$ui/CopyButton.svelte';
  import CommandBlock from './CommandBlock.svelte';
  import { installCommand, KEY_MASK, syncUrl } from './node-logic';

  type Props = {
    detail: NodeDetail;
    /** Lead the page with the install command, for nodes that are not installed yet. */
    hero?: boolean;
    /** Whether the key is readable on screen. Local to the page; never refetches. */
    revealed?: boolean;
  };
  let { detail, hero = false, revealed = $bindable(false) }: Props = $props();
  const node = $derived(detail.node);
  const url = $derived(syncUrl(detail.public_url, detail.sync_path, location.origin));
  const command = $derived(installCommand(detail.installer_url, url, node.key, node.desired_mode));
  const masked = $derived(installCommand(detail.installer_url, url, KEY_MASK, node.desired_mode));
  const insecure = $derived(node.desired_mode === 'active' && !url.startsWith('https://'));
</script>

{#snippet toggle()}
  <Button size="sm" variant="ghost" onclick={() => (revealed = !revealed)} aria-pressed={revealed}>
    {#if revealed}<EyeOff />隐藏密钥{:else}<Eye />显示密钥{/if}
  </Button>
{/snippet}

{#snippet rows()}
  <dl class="grid grid-cols-1 gap-x-4 gap-y-2.5 text-[0.8667rem] sm:grid-cols-[4.5rem_minmax(0,1fr)]">
    <dt class="text-fg-muted sm:pt-1.5">同步地址</dt>
    <dd class="min-w-0"><CommandBlock text={url} /></dd>
    <dt class="text-fg-muted sm:pt-1.5">密钥</dt>
    <dd class="flex min-w-0 items-start gap-1.5">
      <code class="block min-w-0 flex-1 rounded-md border bg-surface-2 px-2.5 py-1.5 font-mono text-xs leading-relaxed break-all {revealed ? 'select-all' : 'tracking-wider text-fg-muted select-none'}">
        {revealed ? node.key : '•'.repeat(32)}
      </code>
      <Button size="sm" variant="ghost" icon class="mt-0.5" onclick={() => (revealed = !revealed)} aria-label={revealed ? '隐藏密钥' : '显示密钥'} title={revealed ? '隐藏密钥' : '显示密钥'} aria-pressed={revealed}>
        {#if revealed}<EyeOff />{:else}<Eye />{/if}
      </Button>
      <CopyButton text={() => node.key} class="mt-0.5" />
    </dd>
    {#if !hero}
      <dt class="text-fg-muted sm:pt-1.5">一键接入</dt>
      <dd class="min-w-0"><CommandBlock text={revealed ? command : masked} copy={command} /></dd>
    {/if}
  </dl>
  <p class="mt-3 text-xs leading-relaxed text-fg-muted">
    {hero ? '' : '在对应节点执行一键接入命令。'}旧节点升级或切换类型也使用一键接入命令，保留节点身份；新主动 Agent 需要 HTTPS。{hero ? '' : '复制的内容始终包含真实密钥。'}
  </p>
{/snippet}

{#if hero}
  <section class="rounded-lg border border-accent/35 bg-surface shadow-sm">
    <div class="flex flex-col gap-3 p-4 sm:p-5">
      <div class="flex items-start gap-3">
        <div class="flex size-9 shrink-0 items-center justify-center rounded-lg bg-accent-soft text-accent-text"><Terminal class="size-4.5" /></div>
        <div class="min-w-0">
          <h2 class="text-[0.9375rem] font-semibold">在节点上执行一键接入命令</h2>
          <p class="mt-0.5 text-[0.8667rem] leading-relaxed text-fg-muted">
            此节点尚未安装。登录这台服务器，粘贴并执行下面的命令；安装完成后本页会自动更新状态。
          </p>
        </div>
      </div>
      <div class="rounded-md border bg-surface-2 p-3">
        <code class="block font-mono text-xs leading-relaxed break-all select-all" data-testid="install-command">{revealed ? command : masked}</code>
      </div>
      <div class="flex flex-wrap items-center gap-2">
        <CopyButton text={() => command} label="复制命令" variant="primary" size="md" />
        {@render toggle()}
        <span class="text-xs text-fg-subtle">复制的内容始终包含真实密钥</span>
      </div>
      {#if insecure}
        <p class="text-xs text-warn-text">主动 Agent 需要通过 HTTPS 连接面板；当前同步地址不是 HTTPS，请在设置中填写 HTTPS 的对外地址。</p>
      {/if}
    </div>
    <div class="border-t p-4 sm:px-5">{@render rows()}</div>
  </section>
{:else}
  <Card title="接入信息">
    {@render rows()}
  </Card>
{/if}
