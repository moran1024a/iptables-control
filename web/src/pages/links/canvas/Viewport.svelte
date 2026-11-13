<script lang="ts" module>
  interface Camera {
    x: number;
    y: number;
    scale: number;
    /** Keep fitting the content while the user has not moved the view. */
    auto: boolean;
    /** The last fit showed everything (button) rather than the readable start view. */
    whole: boolean;
  }
  // One camera per link for the lifetime of the tab, so returning to a link keeps the view.
  const cameras = new Map<string, Camera>();
  export const MAX_SCALE = 3;
</script>

<script lang="ts">
  import type { Snippet } from 'svelte';

  type Rect = { x: number; y: number; width: number; height: number };
  type Props = {
    /** Camera memory key, e.g. the link id. */
    name: string;
    /** World rectangle to fit. */
    bounds: Rect;
    label?: string;
    /** Rendered inside the moving, scaling layer in world coordinates. */
    children: Snippet;
    /** Rendered above the canvas in viewport coordinates. */
    overlay?: Snippet;
    /** A click or tap on empty canvas. */
    onbackground?: () => void;
    /** The camera moved by a user gesture. */
    onmove?: () => void;
    /** Live zoom factor for display. */
    scale?: number;
    /** Re-fit when the content bounds change (until the user moves the view). Off while editing. */
    autofit?: boolean;
    class?: string;
    el?: HTMLDivElement;
  };
  let { name, bounds, label = '拓扑画布', children, overlay, onbackground, onmove, scale = $bindable(1), autofit = true, class: klass = '', el = $bindable() }: Props = $props();

  let layer = $state<HTMLDivElement>();
  let grabbing = $state(false);
  // svelte-ignore state_referenced_locally
  const view: Camera = cameras.get(name) ?? { x: 0, y: 0, scale: 1, auto: true, whole: false };
  // svelte-ignore state_referenced_locally
  cameras.set(name, view);

  const fitScale = () => (el ? Math.min(1, (el.clientWidth - 24) / bounds.width, (el.clientHeight - 24) / bounds.height) : 1);
  const clamp = (value: number) => Math.max(Math.min(0.1, fitScale()), Math.min(MAX_SCALE, value));

  // The transform is written straight to the element: panning never re-renders cards or wires.
  function apply() {
    if (!layer || !el) return;
    layer.style.transform = `translate(${view.x}px, ${view.y}px) scale(${view.scale})`;
    // Zoomed far out, a 24 px dot grid turns into noise: thin it out.
    const grid = 24 * view.scale * (view.scale < 0.3 ? 4 : view.scale < 0.6 ? 2 : 1);
    el.style.backgroundSize = `${grid}px ${grid}px`;
    el.style.backgroundPosition = `${view.x}px ${view.y}px`;
    scale = view.scale;
  }

  /**
   * Show all content. Automatic fits on a narrow screen stop at a readable size instead and start at the
   * left edge (the entries); the button always shows everything.
   */
  export function fit(whole = true) {
    if (!el) return;
    const all = Math.max(0.05, fitScale());
    const readable = !whole && el.clientWidth < 640 && all < 0.5;
    view.scale = readable ? 0.5 : all;
    view.x = readable ? 12 - bounds.x * view.scale : (el.clientWidth - bounds.width * view.scale) / 2 - bounds.x * view.scale;
    view.y = (el.clientHeight - bounds.height * view.scale) / 2 - bounds.y * view.scale;
    if (readable && bounds.height * view.scale > el.clientHeight) view.y = 12 - bounds.y * view.scale;
    view.auto = true;
    view.whole = whole;
    apply();
  }

  /** Zoom to an absolute factor around a viewport point (default: the centre). */
  export function zoomTo(next: number, at?: { x: number; y: number }) {
    if (!el) return;
    const point = at ?? { x: el.clientWidth / 2, y: el.clientHeight / 2 };
    const target = clamp(next);
    const ratio = target / view.scale;
    view.x = point.x - (point.x - view.x) * ratio;
    view.y = point.y - (point.y - view.y) * ratio;
    view.scale = target;
    view.auto = false;
    apply();
  }
  export const zoomBy = (factor: number, at?: { x: number; y: number }) => zoomTo(view.scale * factor, at);

  export function panBy(dx: number, dy: number) {
    view.x += dx;
    view.y += dy;
    view.auto = false;
    apply();
  }

  /** Client coordinates to world coordinates. */
  export function toWorld(clientX: number, clientY: number): { x: number; y: number } {
    const rect = el!.getBoundingClientRect();
    return { x: (clientX - rect.left - view.x) / view.scale, y: (clientY - rect.top - view.y) / view.scale };
  }
  /** World coordinates to viewport-relative coordinates. */
  export function toView(x: number, y: number): { x: number; y: number } {
    return { x: x * view.scale + view.x, y: y * view.scale + view.y };
  }
  export const centre = () => toWorld(el!.getBoundingClientRect().left + el!.clientWidth / 2, el!.getBoundingClientRect().top + el!.clientHeight / 2);
  export const currentScale = () => view.scale;

  /** Scroll a world rectangle into view with a margin. */
  export function reveal(box: Rect, margin = 24) {
    if (!el) return;
    const a = toView(box.x, box.y);
    const b = toView(box.x + box.width, box.y + box.height);
    let dx = 0;
    let dy = 0;
    if (a.x < margin) dx = margin - a.x;
    else if (b.x > el.clientWidth - margin) dx = Math.max(margin - a.x, el.clientWidth - margin - b.x);
    if (a.y < margin) dy = margin - a.y;
    else if (b.y > el.clientHeight - margin) dy = Math.max(margin - a.y, el.clientHeight - margin - b.y);
    if (dx || dy) panBy(dx, dy);
  }

  /** While dragging something near an edge, scroll the camera. Returns the applied shift. */
  export function edgePan(clientX: number, clientY: number): { dx: number; dy: number } {
    if (!el) return { dx: 0, dy: 0 };
    const rect = el.getBoundingClientRect();
    const speed = (distance: number) => (distance < 36 ? Math.round(((36 - Math.max(0, distance)) / 36) * 10) : 0);
    const dx = speed(clientX - rect.left) - speed(rect.right - clientX);
    const dy = speed(clientY - rect.top) - speed(rect.bottom - clientY);
    if (dx || dy) panBy(dx, dy);
    return { dx, dy };
  }

  // Fit on first show and whenever the size changes while the user has not moved the view.
  $effect(() => {
    if (!el) return;
    const observer = new ResizeObserver(() => (view.auto ? fit(view.whole) : apply()));
    observer.observe(el);
    return () => observer.disconnect();
  });
  // New content bounds: keep fitting when automatic.
  $effect(() => {
    void bounds.x, bounds.y, bounds.width, bounds.height;
    if (view.auto && autofit) fit(view.whole);
  });

  const pointers = new Map<number, { x: number; y: number }>();
  let gesture: { moved: boolean; startX: number; startY: number; distance: number } | null = null;
  let swallowClick = false;

  const interactive = (target: EventTarget | null) => target instanceof Element && !!target.closest('[data-nopan], input, select, textarea, a[href]');

  function pointerdown(event: PointerEvent) {
    if (event.button !== 0 || interactive(event.target)) return;
    pointers.set(event.pointerId, { x: event.clientX, y: event.clientY });
    if (pointers.size === 1) gesture = { moved: false, startX: event.clientX, startY: event.clientY, distance: 0 };
    else if (pointers.size === 2 && gesture) {
      const [a, b] = [...pointers.values()];
      gesture.distance = Math.hypot(a.x - b.x, a.y - b.y);
      gesture.moved = true;
    }
  }

  function pointermove(event: PointerEvent) {
    const previous = pointers.get(event.pointerId);
    if (!previous || !gesture || !el) return;
    const current = { x: event.clientX, y: event.clientY };
    if (pointers.size === 1) {
      if (!gesture.moved && Math.hypot(current.x - gesture.startX, current.y - gesture.startY) < 4) return;
      if (!gesture.moved) {
        gesture.moved = true;
        grabbing = true;
        el.setPointerCapture(event.pointerId);
        onmove?.();
      }
      panBy(current.x - previous.x, current.y - previous.y);
    } else if (pointers.size === 2) {
      pointers.set(event.pointerId, current);
      const [a, b] = [...pointers.values()];
      const distance = Math.hypot(a.x - b.x, a.y - b.y);
      const rect = el.getBoundingClientRect();
      const middle = { x: (a.x + b.x) / 2 - rect.left, y: (a.y + b.y) / 2 - rect.top };
      if (gesture.distance > 0) zoomBy(distance / gesture.distance, middle);
      gesture.distance = distance;
      onmove?.();
    }
    pointers.set(event.pointerId, current);
  }

  function pointerup(event: PointerEvent) {
    if (!pointers.delete(event.pointerId)) return;
    if (pointers.size) return;
    const moved = gesture?.moved ?? false;
    gesture = null;
    grabbing = false;
    if (moved) {
      // The click that follows a drag must not activate the card under the pointer.
      swallowClick = true;
      setTimeout(() => (swallowClick = false), 0);
    } else if (event.type === 'pointerup' && event.target instanceof Element && !event.target.closest('[data-card], button, a')) onbackground?.();
  }

  function clickCapture(event: MouseEvent) {
    if (!swallowClick) return;
    swallowClick = false;
    event.stopPropagation();
    event.preventDefault();
  }

  function wheel(event: WheelEvent) {
    if (!el) return;
    if (event.target instanceof Element && event.target.closest('[data-scroll]')) return;
    event.preventDefault();
    const rect = el.getBoundingClientRect();
    // A mouse wheel zooms; a trackpad scrolls the view and pinches (reported with ctrl) to zoom.
    const wheelLike = event.deltaMode !== 0 || (event.deltaX === 0 && Number.isInteger(event.deltaY) && Math.abs(event.deltaY) >= 50);
    if (event.ctrlKey || event.metaKey || wheelLike) {
      const delta = event.deltaMode === 1 ? event.deltaY * 33 : event.deltaY;
      const rate = event.ctrlKey && !wheelLike ? 0.01 : 0.002;
      zoomBy(Math.exp(-Math.max(-300, Math.min(300, delta)) * rate), { x: event.clientX - rect.left, y: event.clientY - rect.top });
    } else panBy(-event.deltaX, -event.deltaY);
    onmove?.();
  }
  // Wheel must be a non-passive listener to stop the page from scrolling.
  $effect(() => {
    if (!el) return;
    el.addEventListener('wheel', wheel, { passive: false });
    return () => el?.removeEventListener('wheel', wheel);
  });

  function keydown(event: KeyboardEvent) {
    if (event.target !== el || event.ctrlKey || event.metaKey || event.altKey) return;
    const pan: Record<string, [number, number]> = { ArrowLeft: [30, 0], ArrowRight: [-30, 0], ArrowUp: [0, 30], ArrowDown: [0, -30] };
    if (event.key === '+' || event.key === '=') zoomBy(1.2);
    else if (event.key === '-') zoomBy(1 / 1.2);
    else if (event.key === '0') fit(true);
    else if (pan[event.key]) panBy(...pan[event.key]);
    else return;
    event.preventDefault();
  }
</script>

<!-- svelte-ignore a11y_no_noninteractive_tabindex, a11y_no_noninteractive_element_interactions -->
<div
  bind:this={el}
  role="application"
  aria-label={label}
  aria-roledescription="可平移缩放的画布"
  tabindex="0"
  class="canvas-viewport relative min-h-0 touch-none overflow-hidden outline-none select-none focus-visible:ring-2 focus-visible:ring-accent/40 focus-visible:ring-inset {grabbing
    ? 'cursor-grabbing'
    : 'cursor-grab'} {klass}"
  onpointerdown={pointerdown}
  onpointermove={pointermove}
  onpointerup={pointerup}
  onpointercancel={pointerup}
  onclickcapture={clickCapture}
  onkeydown={keydown}
>
  <div bind:this={layer} class="absolute top-0 left-0 size-0 origin-top-left {grabbing ? 'will-change-transform' : ''}">
    {@render children()}
  </div>
  {@render overlay?.()}
</div>

<style>
  .canvas-viewport {
    background-color: var(--surface-2);
    background-image: radial-gradient(circle, var(--border-strong) 1px, transparent 1.2px);
    background-size: 24px 24px;
  }
</style>
