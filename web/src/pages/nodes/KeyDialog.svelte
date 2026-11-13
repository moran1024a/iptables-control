<script lang="ts">
  import Alert from '$ui/Alert.svelte';
  import Button from '$ui/Button.svelte';
  import Dialog from '$ui/Dialog.svelte';
  import CommandBlock from './CommandBlock.svelte';

  /** Shows a freshly issued node key once, with what to do with it. */
  let { open = $bindable(false), key, name }: { open?: boolean; key: string; name: string } = $props();
</script>

<Dialog bind:open title="密钥已重置" size="md">
  <div class="flex flex-col gap-3">
    <Alert tone="warn">旧密钥已失效，{name} 在更新密钥前无法继续同步。</Alert>
    <div>
      <p class="mb-1.5 text-[0.8667rem] text-fg-muted">1. 在节点上运行下面的命令：</p>
      <CommandBlock text="sudo ictl --credentials" prompt />
    </div>
    <div>
      <p class="mb-1.5 text-[0.8667rem] text-fg-muted">2. 按提示填入新密钥：</p>
      <CommandBlock text={key} copyLabel="复制" />
    </div>
    <p class="text-xs text-fg-muted">之后仍可在节点页面的「接入信息」中查看此密钥，也可以直接重新执行一键接入命令。</p>
  </div>
  {#snippet footer()}
    <Button variant="primary" onclick={() => (open = false)}>完成</Button>
  {/snippet}
</Dialog>
