export interface ShortcutEvent {
  key: string;
  metaKey?: boolean;
  ctrlKey?: boolean;
  shiftKey?: boolean;
  altKey?: boolean;
  repeat?: boolean;
  isComposing?: boolean;
  defaultPrevented?: boolean;
  target?: unknown;
}
export type StudioShortcut =
  | 'palette'
  | 'save'
  | 'transport'
  | 'undo'
  | 'redo'
  | 'split'
  | 'duplicate'
  | 'delete';
export function studioShortcut(event: ShortcutEvent, timeline: boolean): StudioShortcut | null {
  if (event.repeat || event.isComposing || event.defaultPrevented) return null;
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
    target?.closest?.(
      '[role="dialog"], [role="alertdialog"], [role="menu"], [role="listbox"], [data-shortcut-capture]',
    )
  )
    return null;
  if (modifier && key === 's' && !event.altKey && !event.shiftKey) return 'save';
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
