import { expect, test } from 'bun:test';
import { appShortcut, attachAppShortcuts, navigationShortcuts } from '../src/lib/keyboardShortcuts';

test('navigation keys use physical digit positions even when Option changes the key character', () => {
  for (const item of navigationShortcuts) {
    expect(
      appShortcut({ key: '¡', code: `Digit${item.digit}`, metaKey: true, altKey: true }, '/'),
    ).toEqual({ type: 'navigate', path: item.path });
    expect(appShortcut({ key: item.digit, ctrlKey: true, altKey: true }, '/voices')).toEqual({
      type: 'navigate',
      path: item.path,
    });
  }
  expect(appShortcut({ key: ',', ctrlKey: true }, '/')).toEqual({
    type: 'navigate',
    path: '/settings',
  });
  expect(appShortcut({ key: 'G', metaKey: true, shiftKey: true }, '/')).toEqual({
    type: 'focus-script',
  });
});

test('help and commands are discoverable without interfering with Studio commands or text editing', () => {
  expect(appShortcut({ key: '/', ctrlKey: true }, '/')).toEqual({ type: 'help' });
  expect(appShortcut({ key: '÷', code: 'Slash', metaKey: true }, '/')).toEqual({ type: 'help' });
  expect(appShortcut({ key: 'k', metaKey: true, target: { tagName: 'TEXTAREA' } }, '/')).toEqual({
    type: 'palette',
  });
  expect(appShortcut({ key: 'k', metaKey: true }, '/studio/projects/p/edit')).toBeNull();
  for (const target of [
    { tagName: 'INPUT' },
    { tagName: 'TEXTAREA' },
    { tagName: 'SELECT' },
    { isContentEditable: true },
    { closest: () => ({}) },
  ])
    expect(
      appShortcut({ key: '2', code: 'Digit2', ctrlKey: true, altKey: true, target }, '/'),
    ).toBeNull();
  expect(appShortcut({ key: 'k', metaKey: true, target: { closest: () => ({}) } }, '/')).toBeNull();
  expect(appShortcut({ key: '2' }, '/')).toBeNull();
});

test('one listener owns only recognized chords and removes itself on teardown', () => {
  const listeners = new Set<(event: KeyboardEvent) => void>();
  const target = {
    addEventListener: (_name: string, listener: (event: KeyboardEvent) => void) =>
      listeners.add(listener),
    removeEventListener: (_name: string, listener: (event: KeyboardEvent) => void) =>
      listeners.delete(listener),
  } as unknown as Window;
  const actions: unknown[] = [];
  let prevented = 0;
  const dispose = attachAppShortcuts(
    target,
    () => '/',
    (action) => actions.push(action),
  );
  const emit = (patch: Record<string, unknown>) => {
    for (const listener of listeners)
      listener({
        key: 'k',
        ctrlKey: true,
        preventDefault: () => prevented++,
        ...patch,
      } as unknown as KeyboardEvent);
  };
  emit({});
  emit({ repeat: true });
  emit({ isComposing: true });
  emit({ defaultPrevented: true });
  emit({ key: 'q' });
  expect(actions).toEqual([{ type: 'palette' }]);
  expect(prevented).toBe(1);
  dispose();
  expect(listeners.size).toBe(0);
});
