import { afterAll, afterEach, expect, mock, test } from 'bun:test';
import { createElement, type ReactNode } from 'react';
import { act, create, type ReactTestRenderer } from 'react-test-renderer';

mock.module('react-i18next', () => ({ useTranslation: () => ({ t: (key: string) => key }) }));
mock.module('../src/components/ui/button', () => ({
  Button: (props: any) => createElement('button', props),
}));
const pass = ({ children }: { children?: ReactNode }) => <>{children}</>;
mock.module('../src/components/ui/dialog', () => ({
  Dialog: ({ open, children }: any) => (open ? <>{children}</> : null),
  DialogContent: pass,
  DialogDescription: pass,
  DialogFooter: pass,
  DialogHeader: pass,
  DialogTitle: pass,
}));
const { ChordPicker } = await import('../src/components/ChordPicker/ChordPicker');
const originalWindow = Object.getOwnPropertyDescriptor(globalThis, 'window');
const target = new EventTarget();
const fakeWindow = Object.assign(target, { setTimeout: () => 0, clearTimeout: () => {} });
Object.defineProperty(globalThis, 'window', { configurable: true, value: fakeWindow });
const saved = mock((_keys: string[]) => {});
const cancel = mock(() => {});
let view: ReactTestRenderer;
const props = (initialKeys: string[], open = true) => ({
  open,
  title: 'Shortcut',
  initialKeys,
  onSave: saved,
  onCancel: cancel,
});
function mount(initialKeys: string[] = []) {
  act(() => {
    view = create(<ChordPicker {...props(initialKeys)} />);
  });
}
function key(type: 'keydown' | 'keyup', code: string, keyName: string) {
  const event = new Event(type, { cancelable: true });
  Object.assign(event, { code, key: keyName });
  act(() => target.dispatchEvent(event));
  return event;
}
function save() {
  const button = view.root.findAllByType('button').find((b) => b.props.children === 'common.save')!;
  act(() => button.props.onClick());
}
afterEach(() => {
  act(() => view?.unmount());
  saved.mockClear();
  cancel.mockClear();
});
afterAll(() => {
  mock.restore();
  if (originalWindow) Object.defineProperty(globalThis, 'window', originalWindow);
  else Reflect.deleteProperty(globalThis, 'window');
});

test('captures a peak chord and saves it after release', () => {
  mount(['MetaRight']);
  expect(key('keydown', 'ControlLeft', 'Control').defaultPrevented).toBe(true);
  key('keydown', 'KeyA', 'a');
  key('keyup', 'KeyA', 'a');
  key('keyup', 'ControlLeft', 'Control');
  save();
  expect(saved).toHaveBeenCalledWith(['ControlLeft', 'KeyA']);
});

test('an equal saved-key array from a parent rerender does not discard an in-progress chord', () => {
  mount(['MetaRight']);
  key('keydown', 'ControlLeft', 'Control');
  key('keydown', 'KeyA', 'a');
  key('keyup', 'KeyA', 'a');
  key('keyup', 'ControlLeft', 'Control');
  act(() => view.update(<ChordPicker {...props(['MetaRight'])} />));
  save();
  expect(saved).toHaveBeenCalledWith(['ControlLeft', 'KeyA']);
});

test('losing window focus clears held keys before a new chord', () => {
  mount();
  key('keydown', 'ControlLeft', 'Control');
  act(() => target.dispatchEvent(new Event('blur')));
  key('keydown', 'KeyB', 'b');
  key('keyup', 'KeyB', 'b');
  save();
  expect(saved).toHaveBeenCalledWith(['KeyB']);
});

test('escape and tab remain available to the dialog and closed pickers do not capture', () => {
  mount(['MetaRight']);
  expect(key('keydown', 'Escape', 'Escape').defaultPrevented).toBe(false);
  expect(key('keydown', 'Tab', 'Tab').defaultPrevented).toBe(false);
  act(() => view.update(<ChordPicker {...props(['MetaRight'], false)} />));
  expect(key('keydown', 'KeyA', 'a').defaultPrevented).toBe(false);
});

test('reopening starts with the newest saved shortcut', () => {
  mount(['MetaRight']);
  key('keydown', 'KeyA', 'a');
  key('keyup', 'KeyA', 'a');
  act(() => view.update(<ChordPicker {...props(['ControlRight'], false)} />));
  act(() => view.update(<ChordPicker {...props(['ControlRight'], true)} />));
  save();
  expect(saved).toHaveBeenCalledWith(['ControlRight']);
});

test('a fresh shorter chord replaces the previous longer chord', () => {
  mount(['MetaRight', 'AltGr', 'Space']);
  key('keydown', 'KeyB', 'b');
  key('keyup', 'KeyB', 'b');
  save();
  expect(saved).toHaveBeenCalledWith(['KeyB']);
});

test('unsupported keys show a warning without changing the saved chord', () => {
  mount(['MetaRight']);
  expect(key('keydown', 'IntlRo', 'ろ').defaultPrevented).toBe(true);
  expect(
    view.root
      .findAllByType('p')
      .some((item) => item.props.children === 'captures.chord.unsupported'),
  ).toBe(true);
  save();
  expect(saved).toHaveBeenCalledWith(['MetaRight']);
  key('keydown', 'KeyB', 'b');
  expect(view.root.findAllByType('p')).toHaveLength(0);
});

test('keyboard activation on dialog buttons is not captured as a shortcut', () => {
  mount(['MetaRight']);
  const event = new Event('keydown', { cancelable: true });
  Object.assign(event, { code: 'Enter', key: 'Enter' });
  Object.defineProperty(event, 'target', {
    value: { closest: (selector: string) => (selector.includes('button') ? {} : null) },
  });
  act(() => target.dispatchEvent(event));
  expect(event.defaultPrevented).toBe(false);
  save();
  expect(saved).toHaveBeenCalledWith(['MetaRight']);
});

test('space key release on a dialog button remains available for activation', () => {
  mount(['MetaRight']);
  const event = new Event('keyup', { cancelable: true });
  Object.assign(event, { code: 'Space', key: ' ' });
  Object.defineProperty(event, 'target', {
    value: { closest: (selector: string) => (selector.includes('button') ? {} : null) },
  });
  act(() => target.dispatchEvent(event));
  expect(event.defaultPrevented).toBe(false);
});
