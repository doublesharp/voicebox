import { afterAll, afterEach, expect, mock, test } from 'bun:test';
import { notifyManager, QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { createElement, type ReactNode } from 'react';
import { act, create, type ReactTestRenderer } from 'react-test-renderer';

let rejectSave: (error: Error) => void = () => {};
const notifications: any[] = [];
let serverUrl = 'http://localhost';
const opened: string[] = [];
const updateRequests: any[] = [];
const api = {
  getCaptureSettings: async () => ({}),
  updateCaptureSettings: (patch: any) => {
    updateRequests.push(patch);
    return new Promise((_resolve, reject) => {
      rejectSave = reject;
    });
  },
};
mock.module('../src/lib/api/client', () => ({ apiClient: api }));
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
mock.module('../src/lib/hooks/useProfiles', () => ({ useProfiles: () => ({ data: [] }) }));
mock.module('../src/lib/hooks/useDictationReadiness', () => ({
  useDictationReadiness: () => ({ allReady: true, missing: [] }),
}));
mock.module('../src/components/ui/use-toast', () => ({
  useToast: () => ({ toast: (item: any) => notifications.push(item) }),
}));
const pass = ({ children }: { children?: ReactNode }) => <>{children}</>;
const empty = () => null;
for (const [path, name] of [
  ['../src/components/AccessibilityGate/AccessibilityGate', 'AccessibilityNotice'],
  ['../src/components/InputMonitoringGate/InputMonitoringGate', 'InputMonitoringNotice'],
  ['../src/components/CapturePill/CapturePill', 'CapturePill'],
  ['../src/components/CapturesTab/DictationReadinessChecklist', 'DictationReadinessChecklist'],
  ['../src/components/MicrophoneSelector/MicrophoneSelector', 'MicrophoneSelector'],
] as const)
  mock.module(path, () => ({ [name]: empty }));
const ChordPicker = (_props: any) => null;
mock.module('../src/components/ChordPicker/ChordPicker', () => ({ ChordPicker }));
mock.module('../src/components/ui/input', () => ({
  Input: (props: any) => createElement('input', props),
}));
mock.module('../src/components/ui/button', () => ({
  Button: (props: any) => createElement('button', props),
}));
mock.module('../src/components/ui/toggle', () => ({ Toggle: empty }));
mock.module('../src/components/ui/select', () => ({
  Select: pass,
  SelectContent: pass,
  SelectItem: pass,
  SelectTrigger: pass,
  SelectValue: empty,
}));
mock.module('../src/components/ui/dropdown-menu', () => ({
  DropdownMenu: pass,
  DropdownMenuContent: pass,
  DropdownMenuItem: pass,
  DropdownMenuLabel: pass,
  DropdownMenuSeparator: empty,
  DropdownMenuTrigger: pass,
}));
mock.module('../src/components/ServerTab/SettingRow', () => ({
  SettingRow: ({ children, action }: any) => (
    <>
      {children}
      {action}
    </>
  ),
  SettingSection: pass,
}));
const { CapturesPage } = await import('../src/components/ServerTab/CapturesPage');
const oldFetch = globalThis.fetch;
const oldWindow = Object.getOwnPropertyDescriptor(globalThis, 'window');
Object.defineProperty(globalThis, 'window', {
  configurable: true,
  value: {
    setTimeout: () => 0,
    clearTimeout: () => {},
    setInterval: () => 0,
    clearInterval: () => {},
  },
});
globalThis.fetch = (async () => Response.json({ directories: [] })) as typeof fetch;
notifyManager.setScheduler((fn) => queueMicrotask(fn));
let view: ReactTestRenderer;
let client: QueryClient;
async function flush() {
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 0));
  });
}
afterEach(() => {
  act(() => view?.unmount());
  client?.clear();
  notifications.length = 0;
  opened.length = 0;
  updateRequests.length = 0;
  serverUrl = 'http://localhost';
  globalThis.fetch = (async () => Response.json({ directories: [] })) as typeof fetch;
});
afterAll(() => {
  mock.restore();
  globalThis.fetch = oldFetch;
  if (oldWindow) Object.defineProperty(globalThis, 'window', oldWindow);
  else Reflect.deleteProperty(globalThis, 'window');
  notifyManager.setScheduler((fn) => setTimeout(fn, 0));
});

test('a failed API key save informs the user without redisplaying the key', async () => {
  client = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: Infinity }, mutations: { retry: false } },
  });
  client.setQueryData(['settings', 'captures'], {
    custom_llm_endpoint: 'https://example.test',
    custom_llm_api_key_configured: false,
  });
  await act(async () => {
    view = create(
      <QueryClientProvider client={client}>
        <CapturesPage />
      </QueryClientProvider>,
    );
  });
  const input = view.root.findByProps({ id: 'customLlmApiKey' });
  act(() => input.props.onChange({ target: { value: 'sensitive-token' } }));
  act(() => input.props.onBlur());
  await flush();
  expect(input.props.value).toBe('');
  await act(async () => rejectSave(new Error('save failed for sensitive-token')));
  await flush();
  expect(notifications).toHaveLength(1);
  expect(notifications[0].variant).toBe('destructive');
  expect(JSON.stringify(notifications)).not.toContain('sensitive-token');
});

test('each queued settings failure is reported', async () => {
  client = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: Infinity }, mutations: { retry: false } },
  });
  client.setQueryData(['settings', 'captures'], {
    custom_llm_endpoint: 'https://example.test',
    custom_llm_api_key_configured: false,
  });
  await act(async () => {
    view = create(
      <QueryClientProvider client={client}>
        <CapturesPage />
      </QueryClientProvider>,
    );
  });
  const key = view.root.findByProps({ id: 'customLlmApiKey' });
  const model = view.root.findByProps({ id: 'customLlmModel' });
  act(() => key.props.onChange({ target: { value: 'sensitive-token' } }));
  act(() => key.props.onBlur());
  act(() => model.props.onChange({ target: { value: 'example-model' } }));
  act(() => model.props.onBlur());
  await flush();
  await act(async () => rejectSave(new Error('first failed')));
  await flush();
  await act(async () => rejectSave(new Error('second failed')));
  await flush();
  expect(notifications).toHaveLength(2);
  expect(JSON.stringify(notifications)).not.toContain('sensitive-token');
});

test('a late response from a previous server cannot replace the current capture folder', async () => {
  const requests: Array<(response: Response) => void> = [];
  globalThis.fetch = (() =>
    new Promise<Response>((resolve) => requests.push(resolve))) as typeof fetch;
  client = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: Infinity }, mutations: { retry: false } },
  });
  client.setQueryData(['settings', 'captures'], {});
  await act(async () => {
    view = create(
      <QueryClientProvider client={client}>
        <CapturesPage />
      </QueryClientProvider>,
    );
  });
  serverUrl = 'http://new-server';
  await act(async () =>
    view.update(
      <QueryClientProvider client={client}>
        <CapturesPage />
      </QueryClientProvider>,
    ),
  );
  expect(requests).toHaveLength(2);
  await act(async () => requests[1](Response.json({ directories: [{ path: '/new/captures' }] })));
  await flush();
  await act(async () => requests[0](Response.json({ directories: [{ path: '/old/captures' }] })));
  await flush();
  const button = view.root
    .findAllByType('button')
    .find((item) =>
      item.props.children?.some?.(
        (child: any) => child === 'settings.captures.storage.folder.open',
      ),
    );
  expect(button).toBeDefined();
  await act(async () => button!.props.onClick());
  expect(opened).toEqual(['/new/captures']);
});

test('changing server clears the old folder while the new server is unavailable', async () => {
  const requests: Array<(response: Response) => void> = [];
  globalThis.fetch = (() =>
    new Promise<Response>((resolve) => requests.push(resolve))) as typeof fetch;
  client = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: Infinity }, mutations: { retry: false } },
  });
  client.setQueryData(['settings', 'captures'], {});
  await act(async () => {
    view = create(
      <QueryClientProvider client={client}>
        <CapturesPage />
      </QueryClientProvider>,
    );
  });
  await act(async () => requests[0](Response.json({ directories: [{ path: '/old/captures' }] })));
  await flush();
  const folderButton = () =>
    view.root
      .findAllByType('button')
      .find((item) =>
        item.props.children?.some?.(
          (child: any) => child === 'settings.captures.storage.folder.open',
        ),
      )!;
  expect(folderButton().props.disabled).toBe(false);
  serverUrl = 'http://new-server';
  await act(async () =>
    view.update(
      <QueryClientProvider client={client}>
        <CapturesPage />
      </QueryClientProvider>,
    ),
  );
  expect(folderButton().props.disabled).toBe(true);
  await act(async () => requests[1](new Response('unavailable', { status: 503 })));
  await flush();
  expect(folderButton().props.disabled).toBe(true);
});

test('folder discovery matches the capture directory, not a parent name', async () => {
  globalThis.fetch = (async () =>
    Response.json({
      directories: [{ path: '/data/captures-backup/generations' }, { path: '/data/captures' }],
    })) as typeof fetch;
  client = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: Infinity }, mutations: { retry: false } },
  });
  client.setQueryData(['settings', 'captures'], {});
  await act(async () => {
    view = create(
      <QueryClientProvider client={client}>
        <CapturesPage />
      </QueryClientProvider>,
    );
  });
  await flush();
  const button = view.root
    .findAllByType('button')
    .find((item) =>
      item.props.children?.some?.(
        (child: any) => child === 'settings.captures.storage.folder.open',
      ),
    )!;
  await act(async () => button.props.onClick());
  expect(opened).toEqual(['/data/captures']);
});


test.each([
  { mode: 'push', saved: ['MetaRight', 'AltRight'], candidate: ['AltGr', 'MetaRight'] },
  { mode: 'toggle', saved: ['ControlRight', 'ShiftRight'], candidate: ['ShiftRight', 'ControlRight'] },
])('rejects a conflicting $mode chord without saving', async ({ mode, saved, candidate }) => {
  client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity }, mutations: { retry: false } } });
  client.setQueryData(['settings', 'captures'], {
    hotkey_enabled: true,
    chord_push_to_talk_keys: mode === 'toggle' ? saved : ['ControlRight'],
    chord_toggle_to_talk_keys: mode === 'push' ? saved : ['Space'],
  });
  await act(async () => { view = create(<QueryClientProvider client={client}><CapturesPage /></QueryClientProvider>); });
  const title = mode === 'push' ? 'settings.captures.dictation.chordPicker.pttTitle' : 'settings.captures.dictation.chordPicker.toggleTitle';
  const changeLabel = mode === 'push' ? 'settings.captures.dictation.pushToTalk.change' : 'settings.captures.dictation.toggle.change';
  const button = view.root.findAllByType('button').find((item) => item.children.includes(changeLabel))!;
  act(() => button.props.onClick());
  const picker = view.root.findAllByType(ChordPicker).find((item) => item.props.title === title)!;
  expect(picker.props.open).toBe(true);
  act(() => picker.props.onSave(candidate));
  await flush();
  expect(picker.props.open).toBe(true);
  expect(updateRequests).toEqual([]);
  expect(notifications).toHaveLength(1);
  expect(notifications[0].description).toBe('settings.captures.dictation.chordPicker.conflictDescription');
});

test('allows a toggle chord that extends the push-to-talk chord', async () => {
  client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity }, mutations: { retry: false } } });
  client.setQueryData(['settings', 'captures'], { hotkey_enabled: true, chord_push_to_talk_keys: ['MetaRight', 'AltGr'], chord_toggle_to_talk_keys: ['Space'] });
  await act(async () => { view = create(<QueryClientProvider client={client}><CapturesPage /></QueryClientProvider>); });
  const picker = view.root.findAllByType(ChordPicker).find((item) => item.props.title === 'settings.captures.dictation.chordPicker.toggleTitle')!;
  act(() => picker.props.onSave(['MetaRight', 'AltRight', 'Space']));
  await flush();
  expect(updateRequests).toEqual([{ chord_toggle_to_talk_keys: ['MetaRight', 'AltRight', 'Space'] }]);
  expect(notifications).toHaveLength(0);
});
