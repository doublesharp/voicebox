import { afterEach, expect, mock, test } from 'bun:test';
import type { ReactNode } from 'react';
import { act, create, type ReactTestRenderer } from 'react-test-renderer';

const navigations: string[] = [];
const navigate = async ({ to }: { to: string }) => {
  navigations.push(to);
};
mock.module('@tanstack/react-router', () => ({
  useNavigate: () => navigate,
  useRouterState: ({ select }: { select: (state: unknown) => unknown }) =>
    select({ location: { pathname: '/' } }),
}));
mock.module('../src/components/ui/dialog', () => ({
  Dialog: ({ open, children }: { open: boolean; children: ReactNode }) =>
    open ? <div>{children}</div> : null,
  DialogContent: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  DialogHeader: ({ children }: { children: ReactNode }) => <header>{children}</header>,
  DialogTitle: ({ children }: { children: ReactNode }) => <h2>{children}</h2>,
  DialogDescription: ({ children }: { children: ReactNode }) => <p>{children}</p>,
}));
const { KeyboardShortcuts } = await import('../src/components/KeyboardShortcuts');
let renderer: ReactTestRenderer;
const originalWindow = globalThis.window;
const listeners = new Map<string, EventListener>();
afterEach(() => {
  act(() => renderer?.unmount());
  globalThis.window = originalWindow;
  listeners.clear();
  navigations.length = 0;
});
function mount() {
  globalThis.window = {
    addEventListener: (name: string, listener: EventListener) => listeners.set(name, listener),
    removeEventListener: (name: string) => listeners.delete(name),
  } as unknown as Window & typeof globalThis;
  act(() => {
    renderer = create(<KeyboardShortcuts />);
  });
}
function chord(key: string) {
  act(() =>
    listeners.get('keydown')?.({ key, ctrlKey: true, preventDefault() {} } as unknown as Event),
  );
}
test('help shortcut and sidebar event open the shortcut reference; unmount removes listeners', () => {
  mount();
  chord('/');
  expect(JSON.stringify(renderer.toJSON())).toContain('Keyboard shortcuts');
  expect(JSON.stringify(renderer.toJSON())).toContain('Split selected clip');
  act(() =>
    listeners.get('voicebox:keyboard-shortcuts')?.(new Event('voicebox:keyboard-shortcuts')),
  );
  expect(renderer.root.findAllByType('table')).toHaveLength(3);
  act(() => renderer.unmount());
  expect(listeners.size).toBe(0);
});
test('palette filters destinations and selects through the router', async () => {
  mount();
  chord('k');
  act(() =>
    renderer.root
      .findByProps({ 'aria-label': 'Filter app commands' })
      .props.onChange({ target: { value: 'voice' } }),
  );
  const commands = renderer.root.findAllByType('button');
  expect(commands).toHaveLength(1);
  expect(commands[0].props.children).toBe('Voices');
  await act(async () => {
    await commands[0].props.onClick();
  });
  expect(navigations).toEqual(['/voices']);
  expect(renderer.toJSON()).toBeNull();
});
