import { expect, test } from 'bun:test';
import { studioShortcut } from '../src/features/studio/keyboard';

test('save is available while editing text, without stealing text undo or modal keys', () => {
  const target = { tagName: 'TEXTAREA', closest: () => null };
  expect(studioShortcut({ key: 's', metaKey: true, target }, false)).toBe('save');
  expect(studioShortcut({ key: 'z', metaKey: true, target }, false)).toBeNull();
  expect(
    studioShortcut({ key: 's', ctrlKey: true, target: { closest: () => ({}) } }, false),
  ).toBeNull();
});

test('held, composing and already handled keys do not repeat project commands', () => {
  expect(studioShortcut({ key: 'd', metaKey: true, repeat: true }, true)).toBeNull();
  expect(studioShortcut({ key: 's', isComposing: true }, true)).toBeNull();
  expect(studioShortcut({ key: 'z', ctrlKey: true, defaultPrevented: true }, false)).toBeNull();
});
