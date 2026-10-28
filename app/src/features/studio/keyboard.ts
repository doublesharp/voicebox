export interface ShortcutEvent {
  key: string;
  metaKey?: boolean;
  ctrlKey?: boolean;
  shiftKey?: boolean;
  altKey?: boolean;
  target?: unknown;
}
export type StudioShortcut =
  | 'palette'
  | 'transport'
  | 'undo'
  | 'redo'
  | 'split'
  | 'duplicate'
  | 'delete';
export function studioShortcut(event: ShortcutEvent, timeline: boolean): StudioShortcut | null {
  const target = event.target as {
    tagName?: string;
    isContentEditable?: boolean;
    closest?: (query: string) => unknown;
  } | null;
  const ownsKeys =
    target &&
    (['INPUT', 'TEXTAREA', 'SELECT'].includes(target.tagName ?? '') ||
      target.isContentEditable ||
      target.closest?.(
        '[contenteditable="true"], [role="dialog"], [role="menu"], [role="menuitem"], [role="combobox"]',
      ));
  const modifier = event.metaKey || event.ctrlKey;
  const key = event.key.toLowerCase();
  if (
    modifier &&
    key === 'k' &&
    !event.altKey &&
    !target?.closest?.('[role="dialog"], [role="menu"]')
  )
    return 'palette';
  if (ownsKeys || event.altKey) return null;
  if (modifier && key === 'z') return event.shiftKey ? 'redo' : 'undo';
  if (modifier && key === 'y') return 'redo';
  if (!modifier && key === ' ') {
    if (
      target &&
      (['BUTTON', 'A', 'SUMMARY'].includes(target.tagName ?? '') ||
        target.closest?.('[role="button"]'))
    )
      return null;
    return 'transport';
  }
  if (!timeline) return null;
  if (modifier && key === 'd') return 'duplicate';
  if (!modifier && key === 's') return 'split';
  if (!modifier && ['delete', 'backspace'].includes(key)) return 'delete';
  return null;
}
