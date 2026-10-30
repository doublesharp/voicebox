import { afterAll, expect, mock, test } from 'bun:test';
import { createMemoryHistory, Outlet, RouterProvider } from '@tanstack/react-router';
import type { ReactNode } from 'react';
import { act, create, type ReactTestRenderer } from 'react-test-renderer';

const oldNavigator = Object.getOwnPropertyDescriptor(globalThis, 'navigator');
Object.defineProperty(globalThis, 'navigator', {
  configurable: true,
  value: { platform: 'MacIntel', userAgent: 'Macintosh' },
});
const restored: Array<Record<string, unknown>> = [];
let downloads: Array<{ model_name: string }> = [];
let desktopPlatform = false;
mock.module('../src/platform/PlatformContext', () => ({
  usePlatform: () => ({ metadata: { isTauri: desktopPlatform } }),
}));
mock.module('../src/components/DesktopToolbar', () => ({
  DesktopToolbar: () => <div data-desktop-toolbar />,
}));
const Frame = ({ children }: { children: ReactNode }) => <div data-frame="app">{children}</div>;
const Studio = ({ children }: { children: ReactNode }) => <div data-frame="studio">{children}</div>;
const page = (name: string) => () => <div data-page={name}>{name}</div>;
mock.module('../src/components/AppFrame/AppFrame', () => ({ AppFrame: Frame }));
mock.module('../src/features/studio/StudioFrame', () => ({ StudioFrame: Studio }));
mock.module('../src/components/KeyboardShortcuts', () => ({ KeyboardShortcuts: () => null }));
mock.module('../src/components/Sidebar', () => ({
  Sidebar: ({
    isMacOS,
    desktop,
    collapsed,
    onToggle,
  }: {
    isMacOS: boolean;
    desktop: boolean;
    collapsed: boolean;
    onToggle: () => void;
  }) => (
    <button
      type="button"
      data-sidebar-mac={isMacOS}
      data-desktop={desktop}
      data-collapsed={collapsed}
      onClick={onToggle}
    />
  ),
}));
mock.module('../src/components/ui/toaster', () => ({ Toaster: () => null }));
mock.module('../src/components/MainEditor/MainEditor', () => ({ MainEditor: page('generate') }));
mock.module('../src/lib/hooks/useGenerationProgress', () => ({ useGenerationProgress() {} }));
mock.module('../src/lib/hooks/useRestoreActiveTasks', () => ({
  MODEL_DISPLAY_NAMES: { qwen: 'Qwen' },
  useRestoreActiveTasks: () => downloads,
}));
mock.module('../src/lib/hooks/useModelDownloadToast', () => ({
  useModelDownloadToast: (options: Record<string, unknown>) => {
    restored.push(options);
  },
}));
mock.module('../src/features/studio/ProjectsPage', () => ({ ProjectsPage: page('projects') }));
mock.module('../src/features/studio/StudioProjectPage', () => ({
  StudioProjectPage: page('workspace'),
}));
mock.module('../src/components/StoriesTab/StoriesTab', () => ({ StoriesTab: page('stories') }));
mock.module('../src/components/VoicesTab/VoicesTab', () => ({ VoicesTab: page('voices') }));
mock.module('../src/components/CapturesTab/CapturesTab', () => ({ CapturesTab: page('captures') }));
mock.module('../src/components/EffectsTab/EffectsTab', () => ({ EffectsTab: page('effects') }));
mock.module('../src/components/ModelsTab/ModelsTab', () => ({ ModelsTab: page('models') }));
mock.module('../src/components/ServerTab/ServerTab', () => ({
  SettingsLayout: () => (
    <div data-page="settings">
      <Outlet />
    </div>
  ),
}));
mock.module('../src/components/ServerTab/GeneralPage', () => ({ GeneralPage: page('general') }));
mock.module('../src/components/ServerTab/GenerationPage', () => ({
  GenerationPage: page('generation-settings'),
}));
mock.module('../src/components/ServerTab/CapturesPage', () => ({
  CapturesPage: page('capture-settings'),
}));
mock.module('../src/components/ServerTab/MCPPage', () => ({ MCPPage: page('mcp-settings') }));
mock.module('../src/components/ServerTab/GpuPage', () => ({ GpuPage: page('gpu-settings') }));
mock.module('../src/components/ServerTab/ChangelogPage', () => ({
  ChangelogPage: page('changelog'),
}));
mock.module('../src/components/ServerTab/LogsPage', () => ({ LogsPage: page('logs') }));
mock.module('../src/components/ServerTab/AboutPage', () => ({ AboutPage: page('about') }));
const { router } = await import('../src/router');

afterAll(() => {
  mock.restore();
  if (oldNavigator) Object.defineProperty(globalThis, 'navigator', oldNavigator);
  else Reflect.deleteProperty(globalThis, 'navigator');
});

test('real router resolves app routes and preserves shell behavior', async () => {
  const history = createMemoryHistory({ initialEntries: ['/'] });
  router.update({ history });
  downloads = [{ model_name: 'qwen' }, { model_name: 'unknown-model' }];
  let view!: ReactTestRenderer;
  await act(async () => {
    await router.load();
    view = create(<RouterProvider router={router} />);
  });
  expect(view.root.findByProps({ 'data-page': 'generate' })).toBeDefined();
  expect(view.root.findByProps({ 'data-frame': 'app' })).toBeDefined();
  expect(view.root.findByProps({ 'data-sidebar-mac': true })).toBeDefined();
  expect(restored).toContainEqual({
    modelName: 'qwen',
    displayName: 'Qwen',
    enabled: true,
    includeTerminalSnapshot: true,
  });
  expect(restored).toContainEqual({
    modelName: 'unknown-model',
    displayName: 'unknown-model',
    enabled: true,
    includeTerminalSnapshot: true,
  });
  for (const [path, name, frame] of [
    ['/studio/projects', 'projects', 'studio'],
    ['/studio/projects/demo/edit', 'workspace', 'studio'],
    ['/stories', 'stories', 'app'],
    ['/voices', 'voices', 'app'],
    ['/captures', 'captures', 'app'],
    ['/effects', 'effects', 'app'],
    ['/models', 'models', 'app'],
    ['/settings', 'general', 'app'],
    ['/settings/generation', 'generation-settings', 'app'],
    ['/settings/captures', 'capture-settings', 'app'],
    ['/settings/mcp', 'mcp-settings', 'app'],
    ['/settings/gpu', 'gpu-settings', 'app'],
    ['/settings/changelog', 'changelog', 'app'],
    ['/settings/logs', 'logs', 'app'],
    ['/settings/about', 'about', 'app'],
  ]) {
    await act(async () => {
      await router.navigate({ to: path });
      view.update(<RouterProvider key={path} router={router} />);
    });
    expect(view.root.findByProps({ 'data-page': name })).toBeDefined();
    expect(view.root.findByProps({ 'data-frame': frame })).toBeDefined();
    const shell = view.root.findByProps({ className: 'app-shell' });
    expect(shell.props['data-project-workspace']).toBe(path === '/studio/projects/demo/edit');
  }
  await act(async () => {
    await router.navigate({ to: '/server' });
    view.update(<RouterProvider key="redirect" router={router} />);
  });
  expect(router.state.statusCode).toBe(307);
  expect(router.state.redirect?.options.href).toBe('/settings');
  await act(async () => {
    view.unmount();
  });
});

test('non-Mac shell does not restore nonexistent downloads', async () => {
  Object.defineProperty(globalThis, 'navigator', {
    configurable: true,
    value: { platform: 'Win32', userAgent: 'Windows' },
  });
  downloads = [];
  restored.length = 0;
  router.update({ history: createMemoryHistory({ initialEntries: ['/'] }) });
  let view!: ReactTestRenderer;
  await act(async () => {
    await router.load();
    view = create(<RouterProvider router={router} />);
  });
  expect(view.root.findByProps({ 'data-sidebar-mac': false })).toBeDefined();
  expect(restored).toHaveLength(0);
  await act(async () => view.unmount());
});

for (const [os, platform, userAgent] of [
  ['macos', 'MacIntel', 'Macintosh'],
  ['windows', 'Win32', 'Windows'],
] as const) {
  test(`${os} desktop router retains sidebar state and routes native menu requests`, async () => {
    const oldWindow = Object.getOwnPropertyDescriptor(globalThis, 'window');
    const oldStorage = Object.getOwnPropertyDescriptor(globalThis, 'localStorage');
    const target = new EventTarget();
    const saved = new Map([['voicebox-desktop:sidebar-width', '220']]);
    Object.defineProperty(globalThis, 'window', { configurable: true, value: target });
    Object.defineProperty(globalThis, 'localStorage', {
      configurable: true,
      value: {
        getItem: (key: string) => saved.get(key) ?? null,
        setItem: (key: string, value: string) => saved.set(key, value),
      },
    });
    Object.defineProperty(globalThis, 'navigator', {
      configurable: true,
      value: { platform, userAgent },
    });
    desktopPlatform = true;
    router.update({ history: createMemoryHistory({ initialEntries: ['/'] }) });
    let view!: ReactTestRenderer;
    try {
      await act(async () => {
        await router.load();
        view = create(<RouterProvider router={router} />);
      });
      const shell = view.root.findByProps({ className: 'app-shell' });
      expect(shell.props['data-desktop-platform']).toBe(os);
      expect(view.root.findByProps({ 'data-desktop-page-content': true })).toBeDefined();
      expect(shell.props.style['--desktop-sidebar-width']).toBe('220px');
      await act(async () => {
        target.dispatchEvent(new Event('voicebox:toggle-sidebar'));
      });
      expect(
        view.root.findByProps({ className: 'app-shell' }).props['data-sidebar-collapsed'],
      ).toBe(true);
      expect(saved.get('voicebox-desktop:sidebar-collapsed')).toBe('true');
      await act(async () => {
        target.dispatchEvent(new CustomEvent('voicebox:menu-navigate', { detail: '/voices' }));
        await router.load();
        view.update(<RouterProvider router={router} />);
      });
      expect(router.state.location.pathname).toBe('/voices');
    } finally {
      await act(async () => view?.unmount());
      desktopPlatform = false;
      if (oldWindow) Object.defineProperty(globalThis, 'window', oldWindow);
      else Reflect.deleteProperty(globalThis, 'window');
      if (oldStorage) Object.defineProperty(globalThis, 'localStorage', oldStorage);
      else Reflect.deleteProperty(globalThis, 'localStorage');
    }
  });
}
