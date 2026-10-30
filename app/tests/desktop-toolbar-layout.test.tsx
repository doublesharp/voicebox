import { expect, mock, test } from 'bun:test';
import { act, create } from 'react-test-renderer';

let path = '/';
mock.module('@tanstack/react-router', () => ({
  useRouterState: ({ select }: { select: (state: unknown) => unknown }) =>
    select({ location: { pathname: path } }),
  Link: ({ children, ...props }: Record<string, unknown>) => (
    <a {...props}>{children as React.ReactNode}</a>
  ),
}));
mock.module('react-i18next', () => ({ useTranslation: () => ({ t: (key: string) => key }) }));
const { DesktopToolbar } = await import('../src/components/DesktopToolbar');

for (const route of ['/', '/voices', '/captures', '/effects', '/models', '/stories']) {
  test(`${route} toolbar does not repeat the page heading`, () => {
    path = route;
    let view!: ReturnType<typeof create>;
    act(() => {
      view = create(<DesktopToolbar />);
    });
    expect(view.root.findAllByProps({ className: 'desktop-toolbar-title' })).toHaveLength(0);
    expect(view.root.findByProps({ 'aria-label': 'Keyboard shortcuts' })).toBeDefined();
    act(() => view.unmount());
  });
}

test('studio retains its context actions and workspace navigation', () => {
  path = '/studio/projects/demo/edit';
  let view!: ReturnType<typeof create>;
  act(() => {
    view = create(<DesktopToolbar />);
  });
  expect(view.root.findByProps({ 'aria-label': 'Back to projects' })).toBeDefined();
  expect(view.root.findByProps({ id: 'desktop-toolbar-context-actions' })).toBeDefined();
  act(() => view.unmount());
});
