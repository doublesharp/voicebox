import { afterAll, expect, mock, test } from 'bun:test';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, create } from 'react-test-renderer';

const trigger = mock(async (_name: string) => {});
const cancel = mock(async (_name: string) => {});
const clearAll = mock(async () => {});
const deleteModel = mock(async (_name: string) => {});
const unloadModel = mock(async (_name: string) => {});
const toast = mock((_value: unknown) => {});
let downloadCallbacks: any;
let serverUrl = 'http://server-a';
let serverRevision = 0;
mock.module('../src/lib/api/client', () => ({
  apiClient: {
    getModelStatus: async () => ({ models: [] }),
    getModelsCacheDir: async () => ({ path: '/models' }),
    getActiveTasks: async () => ({ downloads: [] }),
    triggerModelDownload: trigger,
    cancelDownload: cancel,
    clearAllTasks: clearAll,
    deleteModel,
    unloadModel,
  },
}));
mock.module('../src/lib/hooks/useModelDownloadToast', () => ({
  useModelDownloadToast: (options: any) => {
    downloadCallbacks = options;
  },
}));
mock.module('../src/platform/PlatformContext', () => ({
  usePlatform: () => ({ metadata: { isTauri: false } }),
}));
const serverState = () => ({ customModelsDir: null, setCustomModelsDir: () => {}, serverUrl, serverRevision });
const useServerStore = Object.assign((selector: any) => selector(serverState()), { getState: serverState });
mock.module('../src/stores/serverStore', () => ({ useServerStore }));
mock.module('../src/components/ui/use-toast', () => ({ useToast: () => ({ toast }) }));
mock.module('react-i18next', () => ({
  useTranslation: () => ({ t: (key: string) => key }),
  Trans: () => null,
}));
mock.module('../src/components/ui/dialog', () => ({
  Dialog: ({ open, children }: any) => (open ? <div>{children}</div> : null),
  DialogContent: ({ children }: any) => <div>{children}</div>,
  DialogHeader: ({ children }: any) => <div>{children}</div>,
  DialogTitle: ({ children }: any) => <div>{children}</div>,
  DialogDescription: ({ children }: any) => <div>{children}</div>,
}));
mock.module('../src/components/ui/alert-dialog', () => {
  const Part = ({ children }: any) => <div>{children}</div>;
  return {
    AlertDialog: ({ open, children }: any) => (open ? <div>{children}</div> : null),
    AlertDialogAction: ({ children, ...props }: any) => <button {...props}>{children}</button>,
    AlertDialogCancel: Part,
    AlertDialogContent: Part,
    AlertDialogDescription: Part,
    AlertDialogFooter: Part,
    AlertDialogHeader: Part,
    AlertDialogTitle: Part,
  };
});
mock.module('../src/components/ui/progress', () => ({
  Progress: ({ value }: any) => <progress value={value} />,
}));
const { ModelManagement } = await import('../src/components/ServerSettings/ModelManagement');
afterAll(() => mock.restore());

const model = (overrides: Record<string, unknown> = {}) => ({
  model_name: 'whisper-small',
  display_name: 'Small',
  downloaded: false,
  loaded: false,
  downloading: false,
  ...overrides,
});
async function setup(models = [model()], downloads: any[] = []) {
  serverRevision = 0;
  trigger.mockClear();
  cancel.mockClear();
  deleteModel.mockClear();
  unloadModel.mockClear();
  toast.mockClear();
  const client = new QueryClient({
    defaultOptions: { queries: { staleTime: Infinity, retry: false } },
  });
  client.setQueryData(['modelStatus'], { models });
  client.setQueryData(['modelsCacheDir'], { path: '/models' });
  client.setQueryData(['activeTasks'], { downloads });
  let view!: ReturnType<typeof create>;
  await act(async () => {
    view = create(
      <QueryClientProvider client={client}>
        <ModelManagement />
      </QueryClientProvider>,
    );
  });
  const button = (label: string) =>
    view.root
      .findAllByType('button')
      .find(
        (n) =>
          n.children.includes(label) ||
          n.findAll((child) => child.type === 'span' && child.children.includes(label)).length > 0,
      )!;
  const open = (label = 'Small') => act(() => button(label).props.onClick());
  const finish = () => {
    act(() => view.unmount());
    client.clear();
  };
  return { client, view, button, open, finish };
}

test('shows active progress without offering a cancellation that cannot stop the download', async () => {
  const s = await setup(
    [model({ downloading: true })],
    [
      {
        model_name: 'whisper-small',
        status: 'downloading',
        current: 512,
        total: 1024,
        progress: 50,
        started_at: '2026-01-01',
      },
    ],
  );
  expect(s.view.root.findAllByType('progress').map((n) => n.props.value)).toEqual([50]);
  s.open();
  expect(s.view.root.findAllByType('progress').map((n) => n.props.value)).toEqual([50, 50]);
  expect(JSON.stringify(s.view.toJSON())).toContain('512.0 B / 1.0 KB (50.0%)');
  const cancelButtons = s.view.root
    .findAllByType('button')
    .filter(
      (n) =>
        n.props.onClick &&
        n.findAllByType('svg').some((svg) => svg.props.className?.includes('lucide-x')),
    );
  expect(cancelButtons).toHaveLength(0);
  expect(cancel).not.toHaveBeenCalled();
  expect(JSON.stringify(s.view.toJSON())).toContain('models.progress.cannotCancel');
  s.finish();
});

test('deleting a downloaded model requires confirmation and reports success', async () => {
  const s = await setup([model({ downloaded: true, size_mb: 1024 })]);
  try {
    s.open();
    act(() => s.button('models.actions.deleteModel').props.onClick());
    expect(deleteModel).not.toHaveBeenCalled();
    await act(async () => { s.button('common.delete').props.onClick(); await new Promise((r) => setTimeout(r, 10)); });
    expect(deleteModel).toHaveBeenCalledWith('whisper-small');
    expect(toast).toHaveBeenCalledWith(expect.objectContaining({ title: 'models.toast.deleted' }));
  } finally { s.finish(); }
});

test('a failed model delete reports the error and leaves the confirmation available', async () => {
  deleteModel.mockImplementationOnce(async () => { throw new Error('disk error'); });
  const s = await setup([model({ downloaded: true })]);
  try {
    s.open();
    act(() => s.button('models.actions.deleteModel').props.onClick());
    await act(async () => { s.button('common.delete').props.onClick(); await new Promise((r) => setTimeout(r, 10)); });
    expect(toast).toHaveBeenCalledWith(expect.objectContaining({ title: 'models.toast.deleteFailed', description: 'disk error' }));
    expect(s.button('common.delete')).toBeDefined();
  } finally { s.finish(); }
});

test('loaded models must be unloaded before deletion', async () => {
  const s = await setup([model({ downloaded: true, loaded: true })]);
  try {
    s.open();
    expect(s.button('models.actions.deleteModel').props.disabled).toBe(true);
    await act(async () => { s.button('models.actions.unload').props.onClick(); await new Promise((r) => setTimeout(r, 10)); });
    expect(unloadModel).toHaveBeenCalledWith('whisper-small');
    expect(toast).toHaveBeenCalledWith(expect.objectContaining({ title: 'models.toast.unloaded' }));
  } finally { s.finish(); }
});

for (const action of ['delete', 'unload'] as const) {
  test(`queued ${action} cannot dispatch to a different server`, async () => {
    const s = await setup([model({ downloaded: true, loaded: action === 'unload' })]);
    try {
      s.open();
      if (action === 'delete') act(() => s.button('models.actions.deleteModel').props.onClick());
      await act(async () => {
        s.button(action === 'delete' ? 'common.delete' : 'models.actions.unload').props.onClick();
        serverUrl = 'http://server-b'; serverRevision++;
        await new Promise((r) => setTimeout(r, 10));
      });
      expect(action === 'delete' ? deleteModel : unloadModel).not.toHaveBeenCalled();
      expect(toast).not.toHaveBeenCalled();
    } finally { s.finish(); serverUrl = 'http://server-a'; serverRevision++; }
  });
  test(`late ${action} failure does not show on the new server`, async () => {
    let reject!: (error: Error) => void;
    const fn = action === 'delete' ? deleteModel : unloadModel;
    fn.mockImplementationOnce(() => new Promise<void>((_resolve, r) => { reject = r; }));
    const s = await setup([model({ downloaded: true, loaded: action === 'unload' })]);
    try {
      s.open();
      if (action === 'delete') act(() => s.button('models.actions.deleteModel').props.onClick());
      await act(async () => { s.button(action === 'delete' ? 'common.delete' : 'models.actions.unload').props.onClick(); await new Promise((r) => setTimeout(r, 0)); });
      serverUrl = 'http://server-b'; serverRevision++;
      await act(async () => { s.view.update(<QueryClientProvider client={s.client}><ModelManagement /></QueryClientProvider>); });
      await act(async () => { reject(new Error('old server failure')); await new Promise((r) => setTimeout(r, 10)); });
      expect(toast).not.toHaveBeenCalled();
    } finally { s.finish(); serverUrl = 'http://server-a'; serverRevision++; }
  });
  test(`late ${action} success does not report success on the new server`, async () => {
    let resolve!: () => void;
    const fn = action === 'delete' ? deleteModel : unloadModel;
    fn.mockImplementationOnce(() => new Promise<void>((r) => { resolve = r; }));
    const s = await setup([model({ downloaded: true, loaded: action === 'unload' })]);
    try {
      s.open();
      if (action === 'delete') act(() => s.button('models.actions.deleteModel').props.onClick());
      await act(async () => { s.button(action === 'delete' ? 'common.delete' : 'models.actions.unload').props.onClick(); await new Promise((r) => setTimeout(r, 0)); });
      serverUrl = 'http://server-b'; serverRevision++;
      await act(async () => { s.view.update(<QueryClientProvider client={s.client}><ModelManagement /></QueryClientProvider>); });
      s.open();
      await act(async () => { resolve(); await new Promise((r) => setTimeout(r, 10)); });
      expect(toast).not.toHaveBeenCalled();
      expect(s.button(action === 'delete' ? 'models.actions.deleteModel' : 'models.actions.unload')).toBeDefined();
    } finally { s.finish(); serverUrl = 'http://server-a'; serverRevision++; }
  });
}

test('download failure is shown and retry invokes the model service', async () => {
  const s = await setup();
  s.open();
  await act(async () => {
    s.button('models.actions.download').props.onClick();
  });
  expect(trigger).toHaveBeenCalledWith('whisper-small');
  act(() => downloadCallbacks.onError('connection lost'));
  expect(JSON.stringify(s.view.toJSON())).toContain('connection lost');
  await act(async () => {
    s.button('models.actions.retry').props.onClick();
  });
  expect(trigger).toHaveBeenCalledTimes(2);
  s.finish();
});

test('detail follows refreshed download state for the selected model', async () => {
  const s = await setup();
  s.open();
  act(() => {
    s.client.setQueryData(['modelStatus'], { models: [model({ downloading: true })] });
    s.client.setQueryData(['activeTasks'], {
      downloads: [
        {
          model_name: 'whisper-small',
          status: 'downloading',
          current: 256,
          total: 1024,
          progress: 25,
          started_at: '2026-01-01',
        },
      ],
    });
  });
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 10));
  });
  expect(s.view.root.findAllByType('progress').map((n) => n.props.value)).toEqual([25, 25]);
  expect(
    s.view.root.findAllByType('button').some((n) => n.children.includes('models.actions.download')),
  ).toBe(false);
  s.finish();
});

test('a rejected start reports the error and leaves download available', async () => {
  trigger.mockImplementationOnce(async () => {
    throw new Error('server unavailable');
  });
  const s = await setup();
  s.open();
  await act(async () => {
    s.button('models.actions.download').props.onClick();
  });
  expect(toast).toHaveBeenCalledWith(
    expect.objectContaining({
      title: 'models.toast.downloadFailed',
      description: 'server unavailable',
      variant: 'destructive',
    }),
  );
  expect(s.button('models.actions.download')).toBeDefined();
  s.finish();
});

test('dismiss failure restores the error and reports the failure', async () => {
  cancel.mockImplementationOnce(async () => {
    throw new Error('cancel failed');
  });
  const s = await setup(
    [model()],
    [
      {
        model_name: 'whisper-small',
        status: 'error',
        error: 'download failed',
        started_at: '2026-01-01',
      },
    ],
  );
  s.open();
  const cancelButton = s.view.root
    .findAllByType('button')
    .find(
      (n) =>
        n.props.onClick &&
        n.findAllByType('svg').some((svg) => svg.props.className?.includes('lucide-x')),
    )!;
  await act(async () => {
    cancelButton.props.onClick();
    await new Promise((resolve) => setTimeout(resolve, 10));
  });
  expect(toast).toHaveBeenCalledWith(
    expect.objectContaining({ title: 'models.toast.cancelFailed', variant: 'destructive' }),
  );
  expect(JSON.stringify(s.view.toJSON())).toContain('download failed');
  s.finish();
});

test('retry clears the previous local error while the download restarts', async () => {
  const s = await setup();
  try {
    s.open();
    await act(async () => { s.button('models.actions.download').props.onClick(); });
    act(() => downloadCallbacks.onError('connection lost'));
    expect(JSON.stringify(s.view.toJSON())).toContain('connection lost');
    await act(async () => { s.button('models.actions.retry').props.onClick(); });
    expect(downloadCallbacks.enabled).toBe(true);
    expect(JSON.stringify(s.view.toJSON())).not.toContain('connection lost');
    expect(JSON.stringify(s.view.toJSON())).toContain('models.progress.cannotCancel');
  } finally { s.finish(); }
});

test('a failed second download does not detach tracking from the first', async () => {
  trigger.mockImplementation(async (name: string) => {
    if (name === 'whisper-base') throw new Error('second failed');
  });
  const s = await setup([model(), model({ model_name: 'whisper-base', display_name: 'Base' })]);
  try {
    s.open('Small');
    await act(async () => { s.button('models.actions.download').props.onClick(); });
    expect(downloadCallbacks.modelName).toBe('whisper-small');
    s.open('Base');
    await act(async () => { s.button('models.actions.download').props.onClick(); });
    expect(toast).toHaveBeenCalledWith(expect.objectContaining({ description: 'second failed' }));
    expect(downloadCallbacks.enabled).toBe(true);
    expect(downloadCallbacks.modelName).toBe('whisper-small');
  } finally {
    s.finish();
    trigger.mockImplementation(async () => {});
  }
});

test('failed dismissal preserves a newer download started while the request was pending', async () => {
  let rejectCancel = (_error: Error) => {};
  cancel.mockImplementation(() => new Promise<void>((_resolve, reject) => { rejectCancel = reject; }));
  const s = await setup(
    [model(), model({ model_name: 'whisper-base', display_name: 'Base' })],
    [{ model_name: 'whisper-small', status: 'error', error: 'download failed', started_at: '2026-01-01' }],
  );
  try {
    s.open('Small');
    const dismiss = s.view.root.findAllByType('button').find((n) => n.props.onClick && n.findAllByType('svg').some((svg) => svg.props.className?.includes('lucide-x')))!;
    await act(async () => { dismiss.props.onClick(); await Promise.resolve(); });
    s.open('Base');
    await act(async () => { s.button('models.actions.download').props.onClick(); });
    expect(downloadCallbacks.modelName).toBe('whisper-base');
    await act(async () => { rejectCancel(new Error('dismiss failed')); await new Promise((resolve) => setTimeout(resolve, 10)); });
    expect(downloadCallbacks.modelName).toBe('whisper-base');
    expect(downloadCallbacks.enabled).toBe(true);
    expect(toast).toHaveBeenCalledWith(expect.objectContaining({ title: 'models.toast.cancelFailed' }));
  } finally {
    s.finish();
    cancel.mockImplementation(async () => {});
  }
});

test('an old dismissal failure does not restore an error after retrying that model', async () => {
  let rejectCancel = (_error: Error) => {};
  cancel.mockImplementation(() => new Promise<void>((_resolve, reject) => { rejectCancel = reject; }));
  const s = await setup();
  try {
    s.open();
    await act(async () => { s.button('models.actions.download').props.onClick(); });
    act(() => downloadCallbacks.onError('old failure'));
    const dismiss = s.view.root.findAllByType('button').find((n) => n.props.onClick && n.findAllByType('svg').some((svg) => svg.props.className?.includes('lucide-x')))!;
    await act(async () => { dismiss.props.onClick(); await Promise.resolve(); });
    await act(async () => { s.button('models.actions.download').props.onClick(); });
    await act(async () => { rejectCancel(new Error('dismiss failed')); await new Promise((resolve) => setTimeout(resolve, 10)); });
    expect(downloadCallbacks.modelName).toBe('whisper-small');
    expect(downloadCallbacks.enabled).toBe(true);
    expect(JSON.stringify(s.view.toJSON())).not.toContain('old failure');
  } finally {
    s.finish();
    cancel.mockImplementation(async () => {});
  }
});

test('failed retry and failed dismissal restore the prior error', async () => {
  let rejectCancel = (_error: Error) => {};
  cancel.mockImplementation(() => new Promise<void>((_resolve, reject) => { rejectCancel = reject; }));
  const s = await setup();
  try {
    s.open();
    await act(async () => { s.button('models.actions.download').props.onClick(); });
    act(() => downloadCallbacks.onError('old failure'));
    const dismiss = s.view.root.findAllByType('button').find((n) => n.props.onClick && n.findAllByType('svg').some((svg) => svg.props.className?.includes('lucide-x')))!;
    await act(async () => { dismiss.props.onClick(); await Promise.resolve(); });
    trigger.mockImplementationOnce(async () => { throw new Error('retry failed'); });
    await act(async () => { s.button('models.actions.download').props.onClick(); });
    await act(async () => { rejectCancel(new Error('dismiss failed')); await new Promise((resolve) => setTimeout(resolve, 10)); });
    expect(JSON.stringify(s.view.toJSON())).toContain('old failure');
  } finally {
    s.finish();
    cancel.mockImplementation(async () => {});
  }
});

test('an older download response cannot replace tracking for a newer successful start', async () => {
  const pending = new Map<string, () => void>();
  trigger.mockImplementation((name: string) => new Promise<void>((resolve) => pending.set(name, resolve)));
  const s = await setup([model(), model({ model_name: 'whisper-base', display_name: 'Base' })]);
  try {
    s.open('Small');
    act(() => { s.button('models.actions.download').props.onClick(); });
    s.open('Base');
    act(() => { s.button('models.actions.download').props.onClick(); });
    await act(async () => { pending.get('whisper-base')!(); await Promise.resolve(); });
    expect(downloadCallbacks.modelName).toBe('whisper-base');
    await act(async () => { pending.get('whisper-small')!(); await Promise.resolve(); });
    expect(downloadCallbacks.modelName).toBe('whisper-base');
  } finally {
    s.finish();
    trigger.mockImplementation(async () => {});
  }
});

test('an older successful start is tracked when the newer request fails', async () => {
  const pending = new Map<string, { resolve: () => void; reject: (error: Error) => void }>();
  trigger.mockImplementation((name: string) => new Promise<void>((resolve, reject) => pending.set(name, { resolve, reject })));
  const s = await setup([model(), model({ model_name: 'whisper-base', display_name: 'Base' })]);
  try {
    s.open('Small');
    act(() => { s.button('models.actions.download').props.onClick(); });
    s.open('Base');
    act(() => { s.button('models.actions.download').props.onClick(); });
    await act(async () => { pending.get('whisper-base')!.reject(new Error('failed')); await Promise.resolve(); });
    await act(async () => { pending.get('whisper-small')!.resolve(); await Promise.resolve(); });
    expect(downloadCallbacks.modelName).toBe('whisper-small');
    expect(downloadCallbacks.enabled).toBe(true);
  } finally {
    s.finish();
    trigger.mockImplementation(async () => {});
  }
});


test('a download response from a previous server cannot start tracking on the new server', async () => {
  let finish = () => {};
  trigger.mockImplementation(() => new Promise<void>((resolve) => { finish = resolve; }));
  const s = await setup();
  try {
    s.open();
    act(() => { s.button('models.actions.download').props.onClick(); });
    serverUrl = 'http://server-b'; serverRevision++;
    await act(async () => { s.view.update(<QueryClientProvider client={s.client}><ModelManagement /></QueryClientProvider>); });
    await act(async () => { finish(); await Promise.resolve(); });
    expect(downloadCallbacks.enabled).toBe(false);
    expect(downloadCallbacks.modelName).toBe('');
  } finally {
    s.finish();
    serverUrl = 'http://server-a'; serverRevision++;
    trigger.mockImplementation(async () => {});
  }
});

test('switching servers clears tracking from the previous server', async () => {
  const s = await setup();
  try {
    s.open();
    await act(async () => { s.button('models.actions.download').props.onClick(); });
    expect(downloadCallbacks.enabled).toBe(true);
    serverUrl = 'http://server-b'; serverRevision++;
    await act(async () => { s.view.update(<QueryClientProvider client={s.client}><ModelManagement /></QueryClientProvider>); });
    expect(downloadCallbacks.enabled).toBe(false);
    expect(downloadCallbacks.modelName).toBe('');
  } finally {
    s.finish();
    serverUrl = 'http://server-a'; serverRevision++;
  }
});

test('a response from an earlier visit to the same server stays stale', async () => {
  let finish = () => {};
  trigger.mockImplementation(() => new Promise<void>((resolve) => { finish = resolve; }));
  const s = await setup();
  try {
    s.open();
    act(() => { s.button('models.actions.download').props.onClick(); });
    serverUrl = 'http://server-b'; serverRevision++;
    await act(async () => { s.view.update(<QueryClientProvider client={s.client}><ModelManagement /></QueryClientProvider>); });
    serverUrl = 'http://server-a'; serverRevision++;
    await act(async () => { s.view.update(<QueryClientProvider client={s.client}><ModelManagement /></QueryClientProvider>); });
    await act(async () => { finish(); await Promise.resolve(); });
    expect(downloadCallbacks.enabled).toBe(false);
    expect(downloadCallbacks.modelName).toBe('');
  } finally {
    s.finish();
    serverUrl = 'http://server-a'; serverRevision++;
    trigger.mockImplementation(async () => {});
  }
});

test('failure from a previous server does not show a new-server error', async () => {
  let fail = (_error: Error) => {};
  trigger.mockImplementation(() => new Promise<void>((_resolve, reject) => { fail = reject; }));
  const s = await setup();
  try {
    s.open();
    act(() => { s.button('models.actions.download').props.onClick(); });
    serverUrl = 'http://server-b'; serverRevision++;
    await act(async () => { s.view.update(<QueryClientProvider client={s.client}><ModelManagement /></QueryClientProvider>); });
    await act(async () => { fail(new Error('old server failed')); await Promise.resolve(); });
    expect(toast).not.toHaveBeenCalled();
    expect(downloadCallbacks.enabled).toBe(false);
  } finally {
    s.finish();
    serverUrl = 'http://server-a'; serverRevision++;
    trigger.mockImplementation(async () => {});
  }
});

test('switching servers closes a model detail opened on the previous server', async () => {
  const s = await setup();
  try {
    s.open();
    expect(s.button('models.actions.download')).toBeDefined();
    serverUrl = 'http://server-b'; serverRevision++;
    await act(async () => { s.view.update(<QueryClientProvider client={s.client}><ModelManagement /></QueryClientProvider>); });
    expect(s.view.root.findAllByType('button').some((n) => n.children.includes('models.actions.download'))).toBe(false);
  } finally {
    s.finish();
    serverUrl = 'http://server-a'; serverRevision++;
  }
});


test('a pending clear-all cannot erase a download started afterwards', async () => {
  let finish = () => {};
  clearAll.mockImplementation(() => new Promise<void>((resolve) => { finish = resolve; }));
  const s = await setup([model(), model({ model_name: 'whisper-base', display_name: 'Base' })], [
    { model_name: 'whisper-small', status: 'error', error: 'old failure', started_at: '2026-01-01' },
  ]);
  try {
    act(() => { s.button('models.problems.clearAll').props.onClick(); });
    s.open('Base');
    await act(async () => { s.button('models.actions.download').props.onClick(); });
    expect(downloadCallbacks.modelName).toBe('whisper-base');
    await act(async () => { finish(); await new Promise((resolve) => setTimeout(resolve, 10)); });
    expect(downloadCallbacks.modelName).toBe('whisper-base');
    expect(downloadCallbacks.enabled).toBe(true);
  } finally {
    s.finish();
    clearAll.mockImplementation(async () => {});
  }
});

test('clear-all from a previous server cannot clear the current server download', async () => {
  let finish = () => {};
  clearAll.mockImplementation(() => new Promise<void>((resolve) => { finish = resolve; }));
  const s = await setup([model(), model({ model_name: 'whisper-base', display_name: 'Base' })], [
    { model_name: 'whisper-small', status: 'error', error: 'old failure', started_at: '2026-01-01' },
  ]);
  try {
    await act(async () => { s.button('models.problems.clearAll').props.onClick(); await Promise.resolve(); });
    serverUrl = 'http://server-b'; serverRevision++;
    await act(async () => { s.view.update(<QueryClientProvider client={s.client}><ModelManagement /></QueryClientProvider>); });
    s.open('Base');
    await act(async () => { s.button('models.actions.download').props.onClick(); });
    await act(async () => { finish(); await new Promise((resolve) => setTimeout(resolve, 10)); });
    expect(downloadCallbacks.modelName).toBe('whisper-base');
    expect(downloadCallbacks.enabled).toBe(true);
  } finally {
    s.finish();
    serverUrl = 'http://server-a'; serverRevision++;
    clearAll.mockImplementation(async () => {});
  }
});


test('an old download response is ignored before the server switch rerenders', async () => {
  let finish = () => {};
  trigger.mockImplementation(() => new Promise<void>((resolve) => { finish = resolve; }));
  const s = await setup();
  try {
    s.open();
    act(() => { s.button('models.actions.download').props.onClick(); });
    serverUrl = 'http://server-b'; serverRevision++;
    await act(async () => { finish(); await Promise.resolve(); });
    expect(downloadCallbacks.enabled).toBe(false);
    expect(downloadCallbacks.modelName).toBe('');
  } finally {
    s.finish(); serverUrl = 'http://server-a'; serverRevision = 0;
    trigger.mockImplementation(async () => {});
  }
});

test('a batched server round trip cannot revive an earlier download request', async () => {
  let finish = () => {};
  trigger.mockImplementation(() => new Promise<void>((resolve) => { finish = resolve; }));
  const s = await setup();
  try {
    s.open();
    act(() => { s.button('models.actions.download').props.onClick(); });
    serverUrl = 'http://server-b'; serverRevision++;
    serverUrl = 'http://server-a'; serverRevision++;
    await act(async () => { finish(); await Promise.resolve(); });
    expect(downloadCallbacks.enabled).toBe(false);
    expect(downloadCallbacks.modelName).toBe('');
  } finally {
    s.finish(); serverUrl = 'http://server-a'; serverRevision = 0;
    trigger.mockImplementation(async () => {});
  }
});

test('an old-server download failure is silent before the new server rerenders', async () => {
  let reject = (_error: Error) => {};
  trigger.mockImplementation(() => new Promise<void>((_resolve, fail) => { reject = fail; }));
  const s = await setup();
  try {
    s.open();
    act(() => { s.button('models.actions.download').props.onClick(); });
    serverUrl = 'http://server-b'; serverRevision++;
    await act(async () => { reject(new Error('old server failure')); await Promise.resolve(); });
    expect(toast).not.toHaveBeenCalled();
    expect(downloadCallbacks.enabled).toBe(false);
  } finally {
    s.finish(); serverUrl = 'http://server-a'; serverRevision = 0;
    trigger.mockImplementation(async () => {});
  }
});

test('a clear-all response from an earlier visit cannot clear a newer download', async () => {
  let finish = () => {};
  clearAll.mockImplementation(() => new Promise<void>((resolve) => { finish = resolve; }));
  const s = await setup([model(), model({ model_name: 'whisper-base', display_name: 'Base' })], [
    { model_name: 'whisper-small', status: 'error', error: 'old failure', started_at: '2026-01-01' },
  ]);
  try {
    await act(async () => { s.button('models.problems.clearAll').props.onClick(); await Promise.resolve(); });
    serverUrl = 'http://server-b'; serverRevision++;
    serverUrl = 'http://server-a'; serverRevision++;
    await act(async () => { s.view.update(<QueryClientProvider client={s.client}><ModelManagement /></QueryClientProvider>); });
    s.open('Base');
    await act(async () => { s.button('models.actions.download').props.onClick(); });
    await act(async () => { finish(); await new Promise((resolve) => setTimeout(resolve, 10)); });
    expect(downloadCallbacks.modelName).toBe('whisper-base');
    expect(downloadCallbacks.enabled).toBe(true);
  } finally {
    s.finish(); serverUrl = 'http://server-a'; serverRevision = 0;
    clearAll.mockImplementation(async () => {});
  }
});
