import { expect, test } from 'bun:test';
import { isProjectWorkspacePath, observeGlobalPlayerHeight } from '../src/lib/constants/ui';

test('player offset tracks mounting, resizing, replacement and removal, then disconnects', () => {
  const oldResize = globalThis.ResizeObserver;
  const oldMutation = globalThis.MutationObserver;
  let resizeCallback = () => {};
  let mutationCallback = () => {};
  let resizeDisconnected = false;
  let mutationDisconnected = false;
  const observed: Element[] = [];
  const unobserved: Element[] = [];
  globalThis.ResizeObserver = class {
    constructor(callback: () => void) {
      resizeCallback = callback;
    }
    observe(element: Element) {
      observed.push(element);
    }
    unobserve(element: Element) {
      unobserved.push(element);
    }
    disconnect() {
      resizeDisconnected = true;
    }
  } as unknown as typeof ResizeObserver;
  globalThis.MutationObserver = class {
    constructor(callback: () => void) {
      mutationCallback = callback;
    }
    observe() {}
    disconnect() {
      mutationDisconnected = true;
    }
  } as unknown as typeof MutationObserver;
  let height = 128;
  const player = { getBoundingClientRect: () => ({ height }) } as Element;
  const replacement = { getBoundingClientRect: () => ({ height: 90 }) } as Element;
  let current: Element | null = null;
  let value = '';
  const shell = {
    querySelector: () => current,
    style: {
      setProperty: (_name: string, next: string) => {
        value = next;
      },
    },
  } as unknown as HTMLElement;
  try {
    const dispose = observeGlobalPlayerHeight(shell);
    expect(value).toBe('0px');
    current = player;
    mutationCallback();
    expect(value).toBe('128px');
    height = 164;
    resizeCallback();
    expect(value).toBe('164px');
    current = replacement;
    mutationCallback();
    expect(value).toBe('90px');
    expect(observed).toEqual([player, replacement]);
    current = null;
    mutationCallback();
    expect(value).toBe('0px');
    expect(unobserved).toEqual([player, replacement]);
    dispose();
    expect(resizeDisconnected && mutationDisconnected).toBe(true);
  } finally {
    globalThis.ResizeObserver = oldResize;
    globalThis.MutationObserver = oldMutation;
  }
});

test('only project workspace routes force the compact rail', () => {
  expect(isProjectWorkspacePath('/studio/projects/a/edit')).toBe(true);
  expect(isProjectWorkspacePath('/studio/projects/a/review/')).toBe(true);
  expect(isProjectWorkspacePath('/studio/projects')).toBe(false);
  expect(isProjectWorkspacePath('/stories')).toBe(false);
  expect(isProjectWorkspacePath('/studio/projects/a')).toBe(false);
});
