import type { ShortcutEvent } from '@/features/studio/keyboard';

export const navigationShortcuts = [
  { path: '/studio/projects', label: 'Projects', digit: '1' },
  { path: '/', label: 'Generate', digit: '2' },
  { path: '/voices', label: 'Voices', digit: '3' },
  { path: '/captures', label: 'Captures', digit: '4' },
  { path: '/effects', label: 'Effects', digit: '5' },
  { path: '/models', label: 'Models', digit: '6' },
  { path: '/stories', label: 'Stories', digit: '7' },
] as const;
export type AppShortcut =
  | { type: 'help' | 'palette' | 'focus-script' }
  | { type: 'navigate'; path: string };

export function appShortcut(
  event: ShortcutEvent & { code?: string },
  pathname: string,
): AppShortcut | null {
  if (
    event.defaultPrevented ||
    event.repeat ||
    event.isComposing ||
    !(event.metaKey || event.ctrlKey)
  )
    return null;
  const target = event.target as {
    tagName?: string;
    isContentEditable?: boolean;
    closest?: (selector: string) => unknown;
  } | null;
  if (
    target?.closest?.(
      '[role="dialog"], [role="alertdialog"], [role="menu"], [role="listbox"], [data-shortcut-capture]',
    )
  )
    return null;
  const key = event.key.toLowerCase();
  if (!event.altKey && (event.code === 'Slash' || key === '/' || key === '?'))
    return { type: 'help' };
  if (!event.altKey && !event.shiftKey && key === 'k') {
    if (/^\/studio\/projects\/[^/]+\/(edit|mix|review)\/?$/.test(pathname)) return null;
    return { type: 'palette' };
  }
  const typing =
    target &&
    (['INPUT', 'TEXTAREA', 'SELECT'].includes(target.tagName ?? '') ||
      target.isContentEditable ||
      target.closest?.(
        '[contenteditable]:not([contenteditable="false"]), [role="textbox"], [role="combobox"]',
      ));
  if (typing) return null;
  if (!event.altKey && !event.shiftKey && key === ',')
    return { type: 'navigate', path: '/settings' };
  if (!event.altKey && event.shiftKey && key === 'g') return { type: 'focus-script' };
  if (event.altKey && !event.shiftKey) {
    const digit = event.code?.match(/^Digit([1-7])$/)?.[1] ?? key;
    const destination = navigationShortcuts.find((item) => item.digit === digit);
    if (destination) return { type: 'navigate', path: destination.path };
  }
  return null;
}

/** Owns only recognized application chords; other listeners retain their keys. */
export function attachAppShortcuts(
  target: Pick<Window, 'addEventListener' | 'removeEventListener'>,
  pathname: () => string,
  execute: (action: AppShortcut) => void,
) {
  const handler = (event: KeyboardEvent) => {
    const action = appShortcut(event, pathname());
    if (!action) return;
    event.preventDefault();
    execute(action);
  };
  target.addEventListener('keydown', handler);
  return () => target.removeEventListener('keydown', handler);
}
