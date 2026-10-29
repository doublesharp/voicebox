import { afterAll, afterEach, expect, mock, test } from 'bun:test';
import { notifyManager, QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { createElement, type ReactNode } from 'react';
import { act, create, type ReactTestRenderer } from 'react-test-renderer';

let serverUrl = 'http://old-server';
let rejectSave: (error: Error) => void = () => {};
const opened: string[] = [];
const notifications: any[] = [];
const initial = {
  max_chunk_chars: 800,
  crossfade_ms: 50,
  normalize_audio: true,
  autoplay_on_generate: false,
};
mock.module('../src/lib/api/client', () => ({
  apiClient: {
    getGenerationSettings: async () => initial,
    updateGenerationSettings: () =>
      new Promise((_resolve, reject) => {
        rejectSave = reject;
      }),
  },
}));
mock.module('react-i18next', () => ({ useTranslation: () => ({ t: (key: string) => key }) }));
mock.module('../src/platform/PlatformContext', () => ({
  usePlatform: () => ({
    filesystem: {
      openPath: async (path: string) => {
        opened.push(path);
      },
    },
  }),
}));
mock.module('../src/stores/serverStore', () => ({
  useServerStore: (selector: any) => selector({ serverUrl }),
}));
mock.module('../src/components/ui/use-toast', () => ({
  useToast: () => ({ toast: (item: any) => notifications.push(item) }),
}));
mock.module('../src/components/ui/button', () => ({
  Button: (props: any) => createElement('button', props),
}));
mock.module('../src/components/ui/slider', () => ({
  Slider: (props: any) => createElement('test-slider', props),
}));
mock.module('../src/components/ui/toggle', () => ({
  Toggle: (props: any) => createElement('test-toggle', props),
}));
const pass = ({ children }: { children?: ReactNode }) => <>{children}</>;
mock.module('../src/components/ServerTab/SettingRow', () => ({
  SettingRow: ({ children, action }: any) => (
    <>
      {children}
      {action}
    </>
  ),
  SettingSection: pass,
}));
const { GenerationPage } = await import('../src/components/ServerTab/GenerationPage');
const oldFetch = globalThis.fetch;
notifyManager.setScheduler((fn) => queueMicrotask(fn));
let view: ReactTestRenderer | undefined;
let client: QueryClient;
async function flush() {
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 0));
  });
}
async function mount() {
  client = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: Infinity }, mutations: { retry: false } },
  });
  client.setQueryData(['settings', 'generation'], initial);
  await act(async () => {
    view = create(
      <QueryClientProvider client={client}>
        <GenerationPage />
      </QueryClientProvider>,
    );
  });
}
function folderButton() {
  return view!.root
    .findAllByType('button')
    .find((item) =>
      item.props.children?.some?.((child: any) => child === 'settings.generation.folder.open'),
    )!;
}
afterEach(() => {
  if (view) act(() => view?.unmount());
  view = undefined;
  client?.clear();
  opened.length = 0;
  notifications.length = 0;
  serverUrl = 'http://old-server';
  globalThis.fetch = oldFetch;
});
afterAll(() => {
  mock.restore();
  notifyManager.setScheduler((fn) => setTimeout(fn, 0));
});

test('failed generation setting saves show an error and restore the slider', async () => {
  globalThis.fetch = (async () => Response.json({ directories: [] })) as typeof fetch;
  await mount();
  const slider = view!.root.findByProps({ id: 'maxChunkChars' });
  act(() => slider.props.onValueChange([1200]));
  act(() => slider.props.onValueCommit([1200]));
  await flush();
  await act(async () => rejectSave(new Error('save failed')));
  await flush();
  expect(notifications).toHaveLength(1);
  expect(notifications[0].variant).toBe('destructive');
  expect(slider.props.value).toEqual([800]);
});

test('the generations folder follows the current server and ignores late responses', async () => {
  const requests: Array<(response: Response) => void> = [];
  globalThis.fetch = (() =>
    new Promise<Response>((resolve) => requests.push(resolve))) as typeof fetch;
  await mount();
  serverUrl = 'http://new-server';
  await act(async () =>
    view!.update(
      <QueryClientProvider client={client}>
        <GenerationPage />
      </QueryClientProvider>,
    ),
  );
  await act(async () =>
    requests[1](Response.json({ directories: [{ path: '/new/generations' }] })),
  );
  await flush();
  await act(async () =>
    requests[0](Response.json({ directories: [{ path: '/old/generations' }] })),
  );
  await flush();
  await act(async () => folderButton().props.onClick());
  expect(opened).toEqual(['/new/generations']);
});

test('folder discovery matches the generations directory, not a parent name', async () => {
  globalThis.fetch = (async () =>
    Response.json({
      directories: [{ path: '/generations-backup/captures' }, { path: '/data/generations' }],
    })) as typeof fetch;
  await mount();
  await flush();
  await act(async () => folderButton().props.onClick());
  expect(opened).toEqual(['/data/generations']);
});

test('the old folder is disabled while the new server is unavailable', async () => {
  const requests: Array<(response: Response) => void> = [];
  globalThis.fetch = (() =>
    new Promise<Response>((resolve) => requests.push(resolve))) as typeof fetch;
  await mount();
  await act(async () =>
    requests[0](Response.json({ directories: [{ path: '/old/generations' }] })),
  );
  await flush();
  expect(folderButton().props.disabled).toBe(false);
  serverUrl = 'http://new-server';
  await act(async () =>
    view!.update(
      <QueryClientProvider client={client}>
        <GenerationPage />
      </QueryClientProvider>,
    ),
  );
  expect(folderButton().props.disabled).toBe(true);
  await act(async () => requests[1](new Response('unavailable', { status: 503 })));
  await flush();
  expect(folderButton().props.disabled).toBe(true);
});
