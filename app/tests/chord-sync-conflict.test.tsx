import { afterAll, afterEach, expect, mock, test } from 'bun:test';
import { act, create, type ReactTestRenderer } from 'react-test-renderer';

let settings: any;
let canRecord = true;
const calls: any[] = [];
const notices: any[] = [];
mock.module('@tauri-apps/api/core', () => ({ invoke: (name: string, args: any) => { calls.push({ name, args }); return Promise.resolve(); } }));
mock.module('react-i18next', () => ({ useTranslation: () => ({ t: (key: string) => key }) }));
mock.module('../src/lib/hooks/useSettings', () => ({ useCaptureSettings: () => ({ settings }) }));
mock.module('../src/lib/hooks/useDictationReadiness', () => ({ useDictationReadiness: () => ({ canRecord }) }));
mock.module('../src/platform/PlatformContext', () => ({ usePlatform: () => ({ metadata: { isTauri: true } }) }));
mock.module('../src/components/ui/use-toast', () => ({ toast: (notice: any) => notices.push(notice) }));
const { useChordSync } = await import('../src/lib/hooks/useChordSync');
function App() { useChordSync(); return null; }
let view: ReactTestRenderer;
afterEach(() => { act(() => view?.unmount()); calls.length = 0; notices.length = 0; canRecord = true; });
afterAll(() => mock.restore());

test('saved conflicting shortcuts disable the old monitor and notify the user', async () => {
  settings = { hotkey_enabled: true, chord_push_to_talk_keys: ['MetaRight', 'AltGr'], chord_toggle_to_talk_keys: ['AltRight', 'MetaRight'] };
  await act(async () => { view = create(<App />); });
  expect(calls).toEqual([{ name: 'disable_hotkey', args: {} }]);
  expect(notices).toHaveLength(1);
  expect(notices[0].description).toBe('settings.captures.dictation.chordPicker.conflictDescription');
});

test('a valid extended toggle chord enables the monitor', async () => {
  settings = { hotkey_enabled: true, chord_push_to_talk_keys: ['MetaRight', 'AltGr'], chord_toggle_to_talk_keys: ['MetaRight', 'AltRight', 'Space'] };
  await act(async () => { view = create(<App />); });
  expect(calls[0].name).toBe('enable_hotkey');
  expect(notices).toHaveLength(0);
});

test('disabled hotkeys remain quiet even if old settings conflict', async () => {
  settings = { hotkey_enabled: false, chord_push_to_talk_keys: ['Space'], chord_toggle_to_talk_keys: ['Space'] };
  await act(async () => { view = create(<App />); });
  expect(calls[0].name).toBe('disable_hotkey');
  expect(notices).toHaveLength(0);
});

const { chordKeysEqual } = await import('../src/lib/utils/keyCodes');
test.each([
  ['Alt', 'AltLeft'], ['AltGr', 'AltRight'], ['Return', 'Enter'],
  ['UpArrow', 'ArrowUp'], ['DownArrow', 'ArrowDown'], ['LeftArrow', 'ArrowLeft'], ['RightArrow', 'ArrowRight'],
  ['BackQuote', 'Backquote'], ['LeftBracket', 'BracketLeft'], ['RightBracket', 'BracketRight'],
  ['SemiColon', 'Semicolon'], ['BackSlash', 'Backslash'], ['Dot', 'Period'], ['Num0', 'Digit0'], ['Num9', 'Digit9'],
])('compares native aliases %s and %s as the same key', (legacy, canonical) => {
  expect(chordKeysEqual([legacy, 'Space'], ['Space', canonical])).toBe(true);
});

test('compares chords as sets while distinguishing different modifiers', () => {
  expect(chordKeysEqual(['AltGr', 'AltRight'], ['AltRight'])).toBe(true);
  expect(chordKeysEqual(['AltLeft'], ['AltRight'])).toBe(false);
  expect(chordKeysEqual(['Space'], ['Space', 'MetaRight'])).toBe(false);
});
