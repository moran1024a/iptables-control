<script lang="ts">
  import Activity from '@lucide/svelte/icons/activity';
  import ArrowRight from '@lucide/svelte/icons/arrow-right';
  import RotateCw from '@lucide/svelte/icons/rotate-cw';
  import ShieldCheck from '@lucide/svelte/icons/shield-check';
  import Trash2 from '@lucide/svelte/icons/trash-2';
  import { ApiError, del, errorMessage, post } from '$lib/api';
  import { confirm } from '$lib/confirm.svelte';
  import { href, router } from '$lib/router.svelte';
  import { toast } from '$lib/toast.svelte';
  import type { Topology } from '$lib/types';
  import Button from '$ui/Button.svelte';
  import LinkStateBadge from '$ui/LinkStateBadge.svelte';
  import Menu from '$ui/Menu.svelte';

  let { link, onchange }: { link: Topology; onchange: () => void | Promise<void> } = $props();

  const entries = $derived(link.graph.vertices.filter((vertex) => vertex.kind === 'entry').length);
  const targets = $derived(link.graph.vertices.filter((vertex) => vertex.kind === 'target').length);
  const configs = $derived(link.configurations.filter((config) => config.state !== 'deleting'));
  const applied = $derived(configs.filter((config) => config.state === 'active').length);
  const deleting = $derived(link.state === 'deleting');
  // The link reports only its first problem; say which configuration it belongs to and how many more there are.
  const failing = $derived(link.configurations.filter((config) => config.error));
  const problem = $derived.by(() => {
    if (!link.error) return '';
    const source = failing.find((config) => config.error === link.error);
    const prefix = source && link.configurations.length > 1 ? `${source.code}：` : '';
    const more = failing.length > 1 ? `（另有 ${failing.length - 1} 项配置有提示）` : '';
    return prefix + link.error + more;
  });
  let busy = $state(false);

  async function retry() {
    busy = true;
    try {
      await post(`topologies/${link.id}/action`, { action: 'retry', revision: link.revision });
      toast.ok('已重试同步');
    } catch (err) {
      // A stale revision only means the list is behind; the reload below brings the current state.
      toast.error(err instanceof ApiError && err.status === 409 ? '链路刚刚发生变化，已刷新，请再试一次' : errorMessage(err));
    } finally {
      await onchange();
      busy = false;
    }
  }

  async function remove() {
    const ok = await confirm({
      title: '删除链路',
      body: `删除「${link.name}」并清理其转发规则？\n${configs.length ? `它生成的 ${configs.length} 个配置会一并删除，客户端将无法再通过这些入口连接。` : ''}`,
      confirmLabel: '删除',
      danger: true,
    });
    if (!ok) return;
    busy = true;
    try {
      const result = await del<{ deleted: boolean }>(`topologies/${link.id}`);
      if (result.deleted) toast.ok('链路已删除');
      else toast.info('正在清理转发规则，节点确认后链路会从列表消失');
    } catch (err) {
      toast.error(errorMessage(err));
    } finally {
      await onchange();
      busy = false;
    }
  }
</script>

<div class="group relative flex items-center gap-x-3 px-4 py-2.5 transition-colors hover:bg-surface-2 {deleting ? 'opacity-75' : ''}">
  <div class="min-w-0 flex-1">
    <div class="flex min-w-0 items-center gap-2">
      <a
        href={href(`routes/${link.id}`)}
        title={link.name}
        class="truncate text-sm font-medium after:absolute after:inset-0 group-hover:text-accent-text"
      >
        {link.name}
      </a>
      {#if link.failover_enabled}
        <span
          class="inline-flex h-[1.125rem] shrink-0 items-center gap-1 rounded-sm bg-accent-soft px-1.5 text-2xs font-medium text-accent-text"
          title="自动灾备已开启：路径上的节点失联后自动切换到备用路径"
        >
          <ShieldCheck class="size-3" />自动灾备
        </span>
      {/if}
    </div>
    <div class="mt-0.5 flex min-w-0 flex-wrap items-center gap-x-1.5 text-xs text-fg-muted">
      <span class="inline-flex items-center gap-1 whitespace-nowrap">
        {entries} 个入口<ArrowRight class="size-3 text-fg-subtle" aria-label="到" />{targets} 个目标
      </span>
      <span class="whitespace-nowrap">
        · {#if configs.length && applied < configs.length && !deleting}{applied}/{configs.length} 个配置已生效{:else}{configs.length} 个配置{/if}
      </span>
      {#if link.cleanup.length}
        <span class="whitespace-nowrap" title="已不再使用的入口端口，等待所在节点确认释放后自动清除">· {link.cleanup.length} 个旧端口待释放</span>
      {/if}
    </div>
    {#if problem}
      <p class="mt-1 text-xs break-words {link.state === 'error' ? 'text-danger-text' : 'text-warn-text'}">{problem}</p>
    {/if}
  </div>
  <div class="relative z-10 flex shrink-0 items-center gap-1.5 max-sm:flex-col-reverse max-sm:items-end">
    {#if link.sync.retry && !deleting}
      <Button size="sm" loading={busy} onclick={retry} title="重新下发未生效的配置">
        {#if !busy}<RotateCw />{/if}重试同步
      </Button>
    {/if}
    <div class="flex items-center gap-1.5">
      <LinkStateBadge state={link.state} />
      <Menu
        label="{link.name} 的更多操作"
        items={[
          { label: '网络质量', icon: Activity, onselect: () => router.go(`quality/topologies/${link.id}`) },
          'separator',
          { label: deleting ? '正在删除' : '删除', icon: Trash2, danger: true, disabled: deleting || busy, onselect: remove },
        ]}
      />
    </div>
  </div>
</div>
