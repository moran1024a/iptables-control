import { toast } from './toast.svelte';

/** Copy text; falls back to a hidden textarea on plain-HTTP panels where the Clipboard API is unavailable. */
export async function copyText(text: string, message = '已复制'): Promise<boolean> {
  try {
    if (navigator.clipboard && window.isSecureContext) await navigator.clipboard.writeText(text);
    else {
      const area = document.createElement('textarea');
      area.value = text;
      area.setAttribute('readonly', '');
      area.style.cssText = 'position:fixed;top:0;left:0;opacity:0';
      document.body.append(area);
      area.select();
      const ok = document.execCommand('copy');
      area.remove();
      if (!ok) throw new Error('copy rejected');
    }
    if (message) toast.ok(message);
    return true;
  } catch {
    toast.error('复制失败，请手动选择文本复制');
    return false;
  }
}
