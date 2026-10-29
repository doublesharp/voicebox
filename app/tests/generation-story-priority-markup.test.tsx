import { afterAll, afterEach, expect, mock, test } from 'bun:test';
import { createRef, Fragment } from 'react';
import { act, create, type ReactTestRenderer } from 'react-test-renderer';

mock.module('framer-motion', () => ({ AnimatePresence: Fragment, motion: { div: 'div' } }));
const { ParalinguisticInput } = await import('../src/components/Generation/ParalinguisticInput');
const oldNode = globalThis.Node;
const oldWindow = globalThis.window;
(globalThis as any).Node = { TEXT_NODE: 3, ELEMENT_NODE: 1 };
(globalThis as any).window = { getSelection: () => null };
let renderer: ReactTestRenderer;
let editor: any;
function mount(props: any) {
  editor = { innerHTML: '', childNodes: [], focus: mock(() => {}) };
  act(() => {
    renderer = create(<ParalinguisticInput {...props} />, {
      createNodeMock: (element) => (element.props.contentEditable !== undefined ? editor : {}),
    });
  });
}
afterEach(() => {
  act(() => renderer?.unmount());
});
test('external markup becomes noneditable badges and escaped plain text', () => {
  mount({ value: '<hello> [LAUGH] & [unknown]', placeholder: 'Speak' });
  expect(editor.innerHTML).toContain('&lt;hello&gt;');
  expect(editor.innerHTML).toContain('data-ptag="[LAUGH]"');
  expect(editor.innerHTML).toContain('contenteditable="false"');
  expect(editor.innerHTML).toContain('&amp; [unknown]');
  expect(renderer.root.findByProps({ role: 'textbox' }).props['aria-multiline']).toBe(true);
  act(() => renderer.update(<ParalinguisticInput value="[sigh]" />));
  expect(editor.innerHTML).toContain('data-ptag="[sigh]"');
});
test('input serializes nested text, line breaks and badges without zero width spacers', () => {
  const change = mock(() => {});
  mount({ onChange: change });
  editor.childNodes = [
    { nodeType: 3, textContent: 'Hi\u200b ' },
    { nodeType: 1, hasAttribute: () => true, getAttribute: () => '[laugh]' },
    { nodeType: 1, hasAttribute: () => false, tagName: 'BR' },
    {
      nodeType: 1,
      hasAttribute: () => false,
      tagName: 'SPAN',
      childNodes: [{ nodeType: 3, textContent: 'again' }],
    },
  ];
  act(() => renderer.root.findByProps({ role: 'textbox' }).props.onInput());
  expect(change).toHaveBeenCalledWith('Hi [laugh]\nagain');
});
test('composition waits for completion and disabled editor is not interactive', () => {
  const change = mock(() => {});
  const ref = createRef<any>();
  mount({ onChange: change, ref });
  editor.childNodes = [{ nodeType: 3, textContent: 'hello' }];
  const box = renderer.root.findByProps({ role: 'textbox' });
  act(() => {
    box.props.onCompositionStart();
    box.props.onInput();
  });
  expect(change).not.toHaveBeenCalled();
  act(() => box.props.onCompositionEnd());
  expect(change).toHaveBeenCalledWith('hello');
  act(() => ref.current.focus());
  expect(editor.focus).toHaveBeenCalled();
  act(() => renderer.update(<ParalinguisticInput disabled placeholder="Speak" />));
  const disabled = renderer.root.findByProps({ 'aria-disabled': true });
  expect(disabled.props.contentEditable).toBe(false);
  expect(disabled.props.tabIndex).toBe(-1);
  expect(disabled.props.onInput).toBeUndefined();
});
afterAll(() => {
  (globalThis as any).Node = oldNode;
  (globalThis as any).window = oldWindow;
});
