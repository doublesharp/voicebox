import { afterEach, expect, mock, test } from 'bun:test';
import { act, create, type ReactTestRenderer } from 'react-test-renderer';

let path = '/';
mock.module('@tanstack/react-router', () => ({
  useRouterState: ({ select }: { select: (state: unknown) => unknown }) =>
    select({ location: { pathname: path } }),
  Link: ({ to, children, ...props }: Record<string, unknown>) => (
    <a href={to as string} {...props}>
      {children as React.ReactNode}
    </a>
  ),
}));
mock.module('react-i18next', () => ({
  useTranslation: () => ({ t: (key: string) => key.split('.')[1] }),
}));
mock.module('../src/platform/PlatformContext', () => ({
  usePlatform: () => ({
    updater: { getStatus: () => ({ available: false }), subscribe: () => () => {} },
  }),
}));
const { Sidebar } = await import('../src/components/Sidebar');
let renderer: ReactTestRenderer;
afterEach(() => act(() => renderer?.unmount()));

for (const [route, current] of [
  ['/', 'generate'],
  ['/studio/projects', 'Projects'],
  ['/studio/projects/p/edit', 'Projects'],
  ['/stories', 'stories'],
  ['/voices', 'voices'],
  ['/captures', 'captures'],
  ['/effects', 'effects'],
  ['/models', 'models'],
  ['/settings/generation', 'settings'],
] as const) {
  test(`${route} has one current navigation destination`, () => {
    path = route;
    act(() => {
      renderer = create(<Sidebar />);
    });
    const links = renderer.root.findAllByType('a');
    const active = links.filter((link) => link.props['aria-current'] === 'page');
    expect(active).toHaveLength(1);
    expect(active[0].props['aria-label']).toBe(current);
    expect(links.some((link) => link.props.href === '/stories')).toBe(route === '/stories');
  });
}
