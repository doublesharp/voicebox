import { expect, test } from 'bun:test';
import {
  canonicalKeyFromEvent,
  defaultChordKeys,
  displayLabelForKey,
  modifierSideHint,
  sortChordKeys,
} from '../src/lib/utils/keyCodes';

function key(code: string) {
  return canonicalKeyFromEvent({ code } as KeyboardEvent);
}

test('browser key codes map to the native vocabulary used to persist shortcuts', () => {
  const aliases: Record<string, string> = {
    AltLeft: 'Alt',
    AltRight: 'AltGr',
    BracketLeft: 'LeftBracket',
    BracketRight: 'RightBracket',
    Semicolon: 'SemiColon',
    Backslash: 'BackSlash',
    Backquote: 'BackQuote',
    Period: 'Dot',
    Enter: 'Return',
    ArrowUp: 'UpArrow',
    ArrowDown: 'DownArrow',
    ArrowLeft: 'LeftArrow',
    ArrowRight: 'RightArrow',
  };
  for (const [code, expected] of Object.entries(aliases)) expect(key(code)).toBe(expected);
  for (const code of [
    'MetaRight',
    'ControlLeft',
    'ShiftRight',
    'KeyA',
    'Digit9',
    'F12',
    'Space',
    'Escape',
    'PageUp',
  ])
    expect(key(code)).toBe(code);
  for (const code of ['', 'Dead', 'KeyAA', 'F13', 'NumpadAdd', 'Unidentified'])
    expect(key(code)).toBeNull();
});

test('default shortcuts are valid, toggle adds space, and key ordering does not mutate input', () => {
  const push = defaultChordKeys('push');
  const toggle = defaultChordKeys('toggle');
  expect(toggle).toEqual([...push, 'Space']);
  for (const k of push) expect(modifierSideHint(k)).toBe('R');
  const unordered = ['KeyB', 'MetaRight', 'AltGr', 'ControlLeft', 'ShiftRight', 'KeyA'];
  expect(sortChordKeys(unordered)).toEqual([
    'ControlLeft',
    'AltGr',
    'ShiftRight',
    'MetaRight',
    'KeyA',
    'KeyB',
  ]);
  expect(unordered[0]).toBe('KeyB');
});

test('labels and modifier side hints make saved shortcuts distinguishable', () => {
  expect(displayLabelForKey('KeyQ')).toBe('Q');
  expect(displayLabelForKey('Num4')).toBe('4');
  expect(displayLabelForKey('Return')).toBe('↵');
  expect(displayLabelForKey('UpArrow')).toBe('↑');
  expect(displayLabelForKey('F12')).toBe('F12');
  expect(displayLabelForKey('UnknownKey')).toBe('UnknownKey');
  for (const name of ['MetaRight', 'AltGr', 'ControlRight', 'ShiftRight'])
    expect(modifierSideHint(name)).toBe('R');
  for (const name of ['MetaLeft', 'Alt', 'ControlLeft', 'ShiftLeft'])
    expect(modifierSideHint(name)).toBe('L');
  expect(modifierSideHint('Space')).toBeNull();
});
