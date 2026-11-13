// Minimal placement for top-layer popovers (menus, tooltips): fixed coordinates that stay inside the viewport.

export type Side = 'top' | 'bottom' | 'left' | 'right';
export type Align = 'start' | 'center' | 'end';

export function place(anchor: DOMRect, box: HTMLElement, side: Side = 'bottom', align: Align = 'start', gap = 6) {
  const width = box.offsetWidth;
  const height = box.offsetHeight;
  const margin = 8;
  const viewWidth = document.documentElement.clientWidth;
  const viewHeight = document.documentElement.clientHeight;
  // Flip to the opposite side when the preferred one does not fit.
  if (side === 'bottom' && anchor.bottom + gap + height > viewHeight - margin && anchor.top - gap - height >= margin) side = 'top';
  else if (side === 'top' && anchor.top - gap - height < margin && anchor.bottom + gap + height <= viewHeight - margin) side = 'bottom';
  else if (side === 'right' && anchor.right + gap + width > viewWidth - margin && anchor.left - gap - width >= margin) side = 'left';
  else if (side === 'left' && anchor.left - gap - width < margin && anchor.right + gap + width <= viewWidth - margin) side = 'right';
  const along = (start: number, size: number, own: number) =>
    align === 'start' ? start : align === 'end' ? start + size - own : start + (size - own) / 2;
  let left: number;
  let top: number;
  if (side === 'top' || side === 'bottom') {
    left = along(anchor.left, anchor.width, width);
    top = side === 'bottom' ? anchor.bottom + gap : anchor.top - gap - height;
  } else {
    top = along(anchor.top, anchor.height, height);
    left = side === 'right' ? anchor.right + gap : anchor.left - gap - width;
  }
  box.style.left = Math.round(Math.max(margin, Math.min(left, viewWidth - width - margin))) + 'px';
  box.style.top = Math.round(Math.max(margin, Math.min(top, viewHeight - height - margin))) + 'px';
  box.dataset.side = side;
}
