<script lang="ts">
  import type { Settings } from '$lib/types';
  import Card from '$ui/Card.svelte';
  import CopyButton from '$ui/CopyButton.svelte';

  let { settings }: { settings: Settings } = $props();
  const components = $derived(settings.components);
  const rows = $derived<{ name: string; note: string; version: string | undefined }[]>([
    { name: '服务端', note: '面板与调度', version: settings.version || components.server },
    { name: '主动 Agent', note: '节点常驻进程，主动连接面板', version: components.active_agent },
    { name: '被动 Agent', note: '由面板推送配置的节点脚本', version: components.passive_agent },
    { name: '唤醒 Agent', note: '被动节点上接收唤醒的小程序', version: components.wake_agent },
    { name: '节点管理脚本', note: '节点上的安装与维护命令', version: components.manager },
    { name: '独立脚本', note: '不接入面板单独使用的 ipt.sh', version: components.standalone },
  ]);
  const others = $derived((components.supported_apis ?? []).filter((api) => api !== components.api));
</script>

<Card title="关于" hint="此服务端附带的各组件版本" flush>
  <div class="overflow-x-auto">
    <table class="data-table">
      <thead>
        <tr><th scope="col">组件</th><th scope="col">版本</th></tr>
      </thead>
      <tbody>
        {#each rows as row (row.name)}
          <tr>
            <td>
              <span class="text-[0.8125rem] text-fg">{row.name}</span>
              <span class="block text-xs text-fg-subtle">{row.note}</span>
            </td>
            <td class="whitespace-nowrap"><code class="text-xs">{row.version || '-'}</code></td>
          </tr>
        {/each}
        <tr>
          <td>
            <span class="text-[0.8125rem] text-fg">同步协议</span>
            <span class="block text-xs text-fg-subtle">服务端与 Agent 之间的接口版本</span>
          </td>
          <td class="whitespace-nowrap">
            <code class="text-xs">v{components.api ?? '-'}</code>
            {#if others.length}<span class="ml-1 text-xs text-fg-subtle">兼容 {others.map((api) => `v${api}`).join('、')}</span>{/if}
          </td>
        </tr>
      </tbody>
    </table>
  </div>
  <dl class="kv items-center border-t px-4 py-3">
    <dt>面板路径</dt>
    <dd class="flex min-w-0 items-center gap-1">
      <code class="truncate text-xs">{settings.panel_path}/</code>
      <CopyButton text={`${settings.panel_path}/`} size="xs" class="shrink-0" />
    </dd>
    <dt>同步路径</dt>
    <dd class="flex min-w-0 items-center gap-1">
      <code class="truncate text-xs">{settings.sync_path}</code>
      <CopyButton text={settings.sync_path} size="xs" class="shrink-0" />
    </dd>
  </dl>
  <p class="border-t px-4 py-2.5 text-xs leading-relaxed text-fg-muted">这两个路径是随机生成的访问前缀，等同于密钥的一部分，请勿公开。</p>
</Card>
