import { afterAll, afterEach, expect, mock, test } from 'bun:test';
import { Fragment } from 'react';
import { act, create, type ReactTestRenderer } from 'react-test-renderer';

mock.module('framer-motion', () => ({ AnimatePresence: Fragment, motion: { div: 'div' } }));
const { ParalinguisticInput } = await import('../src/components/Generation/ParalinguisticInput');
const oldNode = Object.getOwnPropertyDescriptor(globalThis, 'Node');
const oldWindow = Object.getOwnPropertyDescriptor(globalThis, 'window');
Object.defineProperty(globalThis, 'Node', {
  configurable: true,
  value: { TEXT_NODE: 3, ELEMENT_NODE: 1 },
});
Object.defineProperty(globalThis, 'window', {
  configurable: true,
  value: { getSelection: () => null },
});
const text = (value: string) => ({ nodeType: 3, textContent: value });
const element = (tagName: string, childNodes: any[] = [], badge?: string) => ({
  nodeType: 1,
  tagName,
  childNodes,
  hasAttribute: () => badge !== undefined,
  getAttribute: () => badge ?? null,
});
let view: ReactTestRenderer;
let editor: { innerHTML: string; childNodes: any[]; focus: () => void };
const changed = mock((_value: string) => {});
function mount(nodes: any[]) {
  editor = { innerHTML: '', childNodes: nodes, focus: () => {} };
  act(() => {
    view = create(<ParalinguisticInput onChange={changed} />, {
      createNodeMock: (node) => (node.props.contentEditable !== undefined ? editor : {}),
    });
  });
}
function input() {
  act(() => view.root.findByProps({ role: 'textbox' }).props.onInput());
}
afterEach(() => {
  act(() => view?.unmount());
  changed.mockClear();
});
afterAll(() => {
  mock.restore();
  if (oldNode) Object.defineProperty(globalThis, 'Node', oldNode);
  else Reflect.deleteProperty(globalThis, 'Node');
  if (oldWindow) Object.defineProperty(globalThis, 'window', oldWindow);
  else Reflect.deleteProperty(globalThis, 'window');
});

test('browser-created line blocks and blank lines survive serialization', () => {
  mount([
    text('First'),
    element('DIV', [text('Second')]),
    element('DIV', [element('BR')]),
    element('DIV', [text('Third')]),
  ]);
  input();
  expect(changed).toHaveBeenCalledWith('First\nSecond\n\nThird');
});

test('paragraph blocks and inline tags preserve their boundaries', () => {
  mount([
    element('P', [text('Hello '), element('SPAN', [], '[laugh]')]),
    element('P', [element('STRONG', [text('Again')])]),
  ]);
  input();
  expect(changed).toHaveBeenCalledWith('Hello [laugh]\nAgain');
});

test.each([
  {
    nodes: [element('DIV', [element('BR')]), element('DIV', [text('Start')])],
    expected: '\nStart',
  },
  {
    nodes: [text('Start'), element('DIV', [element('BR')]), element('DIV', [element('BR')])],
    expected: 'Start\n\n',
  },
  {
    nodes: [element('DIV', [text('First'), element('BR')]), element('DIV', [text('Second')])],
    expected: 'First\nSecond',
  },
  {
    nodes: [element('DIV', [text('First')]), element('BR'), text('Second')],
    expected: 'First\nSecond',
  },
  { nodes: [element('DIV', [text('First')]), text('Second')], expected: 'First\nSecond' },
  {
    nodes: [text('Hi\u200b '), element('SPAN', [], '[sigh]'), element('BR'), text('Again')],
    expected: 'Hi [sigh]\nAgain',
  },
])('preserves line edge cases: $expected', ({ nodes, expected }) => {
  mount(nodes);
  input();
  expect(changed).toHaveBeenCalledWith(expected);
});

test('disabling during composition blocks the late event and permits later input', () => {
  mount([text('pending')]);
  const box = view.root.findByProps({ role: 'textbox' });
  act(() => box.props.onCompositionStart());
  act(() => view.update(<ParalinguisticInput disabled onChange={changed} />));
  const disabled = view.root.findByProps({ 'aria-disabled': true });
  expect(disabled.props.onCompositionEnd).toBeUndefined();
  expect(changed).not.toHaveBeenCalled();
  act(() => view.update(<ParalinguisticInput onChange={changed} />));
  input();
  expect(changed).toHaveBeenCalledWith('pending');
});
