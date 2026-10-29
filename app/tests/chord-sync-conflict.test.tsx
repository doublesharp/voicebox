import { afterAll, afterEach, expect, mock, test } from 'bun:test';
import { act, create, type ReactTestRenderer } from 'react-test-renderer';

let settings: any;
let canRecord = true;
const calls: any[] = [];
const notices: any[] = [];
let handler: (name: string, args: any) => Promise<void> = async () => {};
mock.module('@tauri-apps/api/core', () => ({ invoke: (name: string, args: any) => { calls.push({ name, args }); return handler(name, args); } }));
mock.module('react-i18next', () => ({ useTranslation: () => ({ t: (key: string) => key }) }));
mock.module('../src/lib/hooks/useSettings', () => ({ useCaptureSettings: () => ({ settings }) }));
mock.module('../src/lib/hooks/useDictationReadiness', () => ({ useDictationReadiness: () => ({ canRecord }) }));
mock.module('../src/platform/PlatformContext', () => ({ usePlatform: () => ({ metadata: { isTauri: true } }) }));
mock.module('../src/components/ui/use-toast', () => ({ toast: (notice: any) => notices.push(notice) }));
const { useChordSync } = await import('../src/lib/hooks/useChordSync');
function App() { useChordSync(); return null; }
let view: ReactTestRenderer;
afterEach(async () => { await act(async () => { view?.unmount(); await new Promise((resolve) => setTimeout(resolve, 0)); }); calls.length = 0; notices.length = 0; canRecord = true; handler = async () => {}; });
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


test('a slow enable cannot override a later readiness disable', async () => {
  settings = { hotkey_enabled: true, chord_push_to_talk_keys: ['MetaRight'], chord_toggle_to_talk_keys: ['MetaRight', 'Space'] };
  let resolveEnable = () => {};
  let armed = false;
  handler = (name) => name === 'enable_hotkey'
    ? new Promise<void>((resolve) => { resolveEnable = () => { armed = true; resolve(); }; })
    : Promise.resolve().then(() => { armed = false; });
  await act(async () => { view = create(<App />); });
  canRecord = false;
  await act(async () => { view.update(<App />); });
  try {
    expect(calls.map((call) => call.name)).toEqual(['enable_hotkey']);
  } finally {
    await act(async () => { resolveEnable(); await Promise.resolve(); });
  }
  expect(calls.map((call) => call.name)).toEqual(['enable_hotkey', 'disable_hotkey']);
  expect(armed).toBe(false);
});

test('a failed enable still permits a queued disable', async () => {
  settings = { hotkey_enabled: true, chord_push_to_talk_keys: ['MetaRight'], chord_toggle_to_talk_keys: ['MetaRight', 'Space'] };
  let rejectEnable = (_error: Error) => {};
  handler = (name) => name === 'enable_hotkey'
    ? new Promise<void>((_resolve, reject) => { rejectEnable = reject; })
    : Promise.resolve();
  const warn = console.warn;
  console.warn = () => {};
  try {
    await act(async () => { view = create(<App />); });
    canRecord = false;
    await act(async () => { view.update(<App />); });
    expect(calls.map((call) => call.name)).toEqual(['enable_hotkey']);
    await act(async () => { rejectEnable(new Error('unavailable')); await Promise.resolve(); });
    expect(calls.map((call) => call.name)).toEqual(['enable_hotkey', 'disable_hotkey']);
  } finally {
    console.warn = warn;
  }
});

test('rapid changes skip obsolete queued bindings and apply the latest', async () => {
  settings = { hotkey_enabled: true, chord_push_to_talk_keys: ['MetaRight'], chord_toggle_to_talk_keys: ['MetaRight', 'Space'] };
  let finishFirst = () => {};
  handler = (name) => name === 'enable_hotkey' && calls.length === 1
    ? new Promise<void>((resolve) => { finishFirst = resolve; })
    : Promise.resolve();
  await act(async () => { view = create(<App />); });
  canRecord = false;
  await act(async () => { view.update(<App />); });
  canRecord = true;
  settings = { ...settings, chord_push_to_talk_keys: ['ControlRight'], chord_toggle_to_talk_keys: ['ControlRight', 'Space'] };
  await act(async () => { view.update(<App />); });
  expect(calls.map((call) => call.name)).toEqual(['enable_hotkey']);
  await act(async () => { finishFirst(); await Promise.resolve(); });
  expect(calls.map((call) => call.name)).toEqual(['enable_hotkey', 'enable_hotkey']);
  expect(calls[1].args.pushToTalk).toEqual(['ControlRight']);
});


test('unmount disables a slow enable after it finishes', async () => {
  settings = { hotkey_enabled: true, chord_push_to_talk_keys: ['MetaRight'], chord_toggle_to_talk_keys: ['MetaRight', 'Space'] };
  let finishFirst = () => {};
  let armed = false;
  handler = (name) => name === 'enable_hotkey'
    ? new Promise<void>((resolve) => { finishFirst = () => { armed = true; resolve(); }; })
    : Promise.resolve().then(() => { armed = false; });
  await act(async () => { view = create(<App />); });
  act(() => view.unmount());
  await act(async () => { finishFirst(); await Promise.resolve(); });
  expect(calls.map((call) => call.name)).toEqual(['enable_hotkey', 'disable_hotkey']);
  expect(armed).toBe(false);
});

test('a replacement mount enables only after the previous teardown', async () => {
  settings = { hotkey_enabled: true, chord_push_to_talk_keys: ['MetaRight'], chord_toggle_to_talk_keys: ['MetaRight', 'Space'] };
  let finishFirst = () => {};
  let armed = false;
  handler = (name) => {
    if (name === 'enable_hotkey' && calls.length === 1) {
      return new Promise<void>((resolve) => { finishFirst = () => { armed = true; resolve(); }; });
    }
    armed = name === 'enable_hotkey';
    return Promise.resolve();
  };
  await act(async () => { view = create(<App />); });
  act(() => view.unmount());
  settings = { ...settings, chord_push_to_talk_keys: ['ControlRight'], chord_toggle_to_talk_keys: ['ControlRight', 'Space'] };
  await act(async () => { view = create(<App />); });
  expect(calls.map((call) => call.name)).toEqual(['enable_hotkey']);
  await act(async () => { finishFirst(); await Promise.resolve(); });
  expect(calls.map((call) => call.name)).toEqual(['enable_hotkey', 'disable_hotkey', 'enable_hotkey']);
  expect(calls[2].args.pushToTalk).toEqual(['ControlRight']);
  expect(armed).toBe(true);
});
