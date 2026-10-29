import { afterAll, expect, mock, test } from 'bun:test';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, create } from 'react-test-renderer';

const trigger = mock(async (_name: string) => {});
const cancel = mock(async (_name: string) => {});
const toast = mock((_value: unknown) => {});
let downloadCallbacks: any;
mock.module('../src/lib/api/client', () => ({
  apiClient: {
    getModelStatus: async () => ({ models: [] }),
    getModelsCacheDir: async () => ({ path: '/models' }),
    getActiveTasks: async () => ({ downloads: [] }),
    triggerModelDownload: trigger,
    cancelDownload: cancel,
    clearAllTasks: async () => {},
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
mock.module('../src/stores/serverStore', () => ({
  useServerStore: (selector: any) =>
    selector({ customModelsDir: null, setCustomModelsDir: () => {} }),
}));
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
    AlertDialogAction: Part,
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
  trigger.mockClear();
  cancel.mockClear();
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
