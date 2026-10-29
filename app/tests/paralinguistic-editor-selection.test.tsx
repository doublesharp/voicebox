import { afterAll, afterEach, beforeEach, expect, mock, test } from 'bun:test';
import { Fragment } from 'react';
import { act, create, type ReactTestRenderer } from 'react-test-renderer';

mock.module('framer-motion', () => ({ AnimatePresence: Fragment, motion: { div: 'div' } }));
mock.module('react-dom', () => ({ createPortal: (children: unknown) => children }));
const originals = new Map(['Node', 'window', 'document'].map((key) => [key, Object.getOwnPropertyDescriptor(globalThis, key)]));
const changes = mock((_value: string) => {});
let view: ReactTestRenderer | undefined;
let editor: any;
let selected: any;
let selection: any;

const textNode = (text: string, owner: any) => ({ nodeType: 3, textContent: text, owner });
function parseHTML(html: string) {
  const result: any[] = [];
  const pattern = /<span data-ptag="([^"]+)"[^>]*>.*?<\/span>/g;
  const decode = (text: string) => text.replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&amp;/g, '&');
  let cursor = 0;
  for (const match of html.matchAll(pattern)) {
    if (match.index! > cursor) result.push(textNode(decode(html.slice(cursor, match.index)), editor));
    const tag = match[1];
    result.push({ nodeType: 1, owner: editor, hasAttribute: (name: string) => name === 'data-ptag', getAttribute: () => tag });
    cursor = match.index! + match[0].length;
  }
  if (cursor < html.length) result.push(textNode(decode(html.slice(cursor)), editor));
  return result;
}
function range(node: any = editor, start = 0, end = start) {
  return {
    startContainer: node, endContainer: node, startOffset: start, endOffset: end,
    cloneRange() { return range(this.startContainer, this.startOffset, this.endOffset); },
    collapse() { this.endOffset = this.startOffset; },
    setStart(n: any, offset: number) { this.startContainer = n; this.startOffset = offset; },
    setEnd(n: any, offset: number) { this.endContainer = n; this.endOffset = offset; },
    selectNodeContents(n: any) { this.startContainer = n; this.endContainer = n; },
    setStartAfter(n: any) { this.startContainer = n.owner; this.endContainer = n.owner; },
    getBoundingClientRect() { return { top: 30, left: 20 }; },
    deleteContents() {
      if (this.startContainer.nodeType === 3 && this.startContainer === this.endContainer) {
        const n = this.startContainer;
        n.textContent = n.textContent.slice(0, this.startOffset) + n.textContent.slice(this.endOffset);
      }
    },
    insertNode(fragment: any) { editor.childNodes.push(...fragment.children); },
  };
}
function select(r: any) {
  selected = r;
  selection = { rangeCount: 1, getRangeAt: () => selected, removeAllRanges() {}, addRange(next: any) { selected = next; } };
}
const documentFixture = {
  body: {},
  createRange: () => range(),
  createElement() {
    const temp: any = { children: [], get firstChild() { return this.children[0] ?? null; } };
    Object.defineProperty(temp, 'innerHTML', { set(value: string) { temp.children = parseHTML(value); for (const node of temp.children) node.parent = temp; } });
    return temp;
  },
  createDocumentFragment() {
    return { children: [] as any[], appendChild(node: any) { node.parent.children.shift(); this.children.push(node); return node; } };
  },
};
Object.defineProperty(globalThis, 'Node', { configurable: true, value: { TEXT_NODE: 3, ELEMENT_NODE: 1 } });
Object.defineProperty(globalThis, 'window', { configurable: true, value: { getSelection: () => selection, innerHeight: 800 } });
Object.defineProperty(globalThis, 'document', { configurable: true, value: documentFixture });
const { ParalinguisticInput } = await import('../src/components/Generation/ParalinguisticInput');

function mount() {
  editor = { childNodes: [], innerHTML: '', focus: mock(() => {}), contains: (node: any) => node === editor || node?.owner === editor };
  act(() => { view = create(<ParalinguisticInput onChange={changes} />, {
    createNodeMock: (element) => element.props.contentEditable !== undefined ? editor : { children: [] },
  }); });
}
const box = () => view!.root.findByProps({ role: 'textbox' });
const paste = (text: string) => {
  const preventDefault = mock(() => {});
  act(() => box().props.onPaste({ preventDefault, clipboardData: { getData: () => text } }));
  expect(preventDefault).toHaveBeenCalledTimes(1);
};
beforeEach(() => { changes.mockClear(); selection = null; selected = null; });
afterEach(() => { if (view) act(() => view?.unmount()); view = undefined; });
afterAll(() => { mock.restore(); for (const [key, descriptor] of originals) { if (descriptor) Object.defineProperty(globalThis, key, descriptor); else Reflect.deleteProperty(globalThis, key); } });

test('paste with no browser selection appends to the script', () => {
  mount();
  editor.childNodes.push(textNode('Existing ', editor));
  paste('new [laugh] words');
  expect(changes).toHaveBeenCalledWith('Existing new [laugh] words');
});

test('paste never deletes a selection belonging to another element', () => {
  mount();
  editor.childNodes.push(textNode('Script ', editor));
  const external = { nodeType: 3, textContent: 'Other field', owner: {} };
  const foreign = range(external, 0, 5);
  foreign.deleteContents = mock(foreign.deleteContents);
  select(foreign);
  paste('pasted');
  expect(foreign.deleteContents).not.toHaveBeenCalled();
  expect(external.textContent).toBe('Other field');
  expect(changes).toHaveBeenCalledWith('Script pasted');
});

test('autocomplete ignores a caret outside the editor', () => {
  mount();
  const external = textNode('/laugh', {});
  select(range(external, 6));
  act(() => box().props.onInput());
  expect(view!.root.findAllByType('button')).toHaveLength(0);
});

test('paste replaces a selection inside the script', () => {
  mount();
  const script = textNode('hello old', editor);
  editor.childNodes.push(script);
  select(range(script, 6, 9));
  paste('new');
  expect(changes).toHaveBeenCalledWith('hello new');
});

test('autocomplete inserts a tag at an editor caret', () => {
  mount();
  const script = textNode('/laugh', editor);
  editor.childNodes.push(script);
  select(range(script, 6));
  act(() => box().props.onInput());
  const button = view!.root.findAllByType('button')[0];
  expect(button).toBeDefined();
  act(() => button.props.onMouseDown({ preventDefault() {} }));
  expect(changes).toHaveBeenLastCalledWith('[laugh]');
});

test('autocomplete does not modify a selection moved to another field', () => {
  mount();
  const script = textNode('/laugh', editor);
  editor.childNodes.push(script);
  select(range(script, 6));
  act(() => box().props.onInput());
  const button = view!.root.findAllByType('button')[0];
  const external = textNode('Other field', {});
  select(range(external, 0, 5));
  act(() => button.props.onMouseDown({ preventDefault() {} }));
  expect(script.textContent).toBe('/laugh');
  expect(external.textContent).toBe('Other field');
  expect(changes).toHaveBeenLastCalledWith('/laugh');
});
