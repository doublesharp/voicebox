import { afterAll, expect, mock, test } from 'bun:test';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, create } from 'react-test-renderer';

let health: any, cuda: any, rocm: any, tauri: boolean;
const getCuda = mock(async () => cuda),
  getRocm = mock(async () => rocm);
const downloadCuda = mock(async () => {}),
  downloadRocm = mock(async () => {});
const deleteCuda = mock(async () => {}),
  deleteRocm = mock(async () => {});
const restart = mock(async () => {}),
  override = mock(async (_backend: string) => {});
mock.module('../src/lib/api/client', () => ({
  apiClient: {
    getCudaStatus: getCuda,
    getRocmStatus: getRocm,
    downloadCudaBackend: downloadCuda,
    downloadRocmBackend: downloadRocm,
    deleteCudaBackend: deleteCuda,
    deleteRocmBackend: deleteRocm,
    getHealth: async () => ({ status: 'healthy' }),
  },
}));
mock.module('../src/lib/hooks/useServer', () => ({ useServerHealth: () => ({ data: health }) }));
mock.module('../src/platform/PlatformContext', () => ({
  usePlatform: () => ({
    metadata: { isTauri: tauri },
    lifecycle: { restartServer: restart, setBackendOverride: override },
  }),
}));
mock.module('../src/stores/serverStore', () => ({
  useServerStore: (selector: any) => selector({ serverUrl: 'http://server' }),
}));
mock.module('react-i18next', () => ({
  useTranslation: () => ({
    t: (key: string, vars?: any) => (vars?.mb ? `${key} ${vars.mb}` : key),
  }),
}));
mock.module('../src/components/ui/progress', () => ({
  Progress: ({ value }: any) => <progress value={value} />,
}));
class FakeEventSource {
  static instances: FakeEventSource[] = [];
  onmessage: any;
  onerror: any;
  closed = false;
  constructor(public url: string) {
    FakeEventSource.instances.push(this);
  }
  close() {
    this.closed = true;
  }
  send(data: unknown) {
    this.onmessage?.({ data: JSON.stringify(data) });
  }
}
const originalEventSource = globalThis.EventSource;
// @ts-expect-error test boundary
globalThis.EventSource = FakeEventSource;
const { GpuPage } = await import('../src/components/ServerTab/GpuPage');
const { GpuAcceleration } = await import('../src/components/ServerSettings/GpuAcceleration');
afterAll(() => {
  globalThis.EventSource = originalEventSource;
  mock.restore();
});
async function setup(Component: any, options: any = {}) {
  health =
    'health' in options
      ? options.health
      : { gpu_available: false, gpu_type: null, backend_variant: 'cpu', supports_rocm: true };
  cuda = options.cuda ?? { available: false, downloading: false };
  rocm = options.rocm ?? { available: false, downloading: false };
  tauri = options.tauri ?? true;
  FakeEventSource.instances = [];
  for (const fn of [
    getCuda,
    getRocm,
    downloadCuda,
    downloadRocm,
    deleteCuda,
    deleteRocm,
    restart,
    override,
  ])
    fn.mockClear();
  const client = new QueryClient({
    defaultOptions: { queries: { staleTime: Infinity, retry: false } },
  });
  client.setQueryData(['cuda-status', 'http://server'], cuda);
  client.setQueryData(['rocm-status', 'http://server'], rocm);
  let view!: ReturnType<typeof create>;
  await act(async () => {
    view = create(
      <QueryClientProvider client={client}>
        <Component />
      </QueryClientProvider>,
    );
  });
  const text = () => JSON.stringify(view.toJSON());
  const button = (label: string) =>
    view.root
      .findAllByType('button')
      .find((n) => n.children.some((child) => typeof child === 'string' && child.includes(label)));
  const click = async (label: string) => {
    const b = button(label);
    expect(b).toBeDefined();
    await act(async () => {
      await b!.props.onClick();
    });
  };
  const finish = () => {
    act(() => view.unmount());
    client.clear();
  };
  return { view, text, button, click, finish };
}
for (const [name, Component, labels] of [
  [
    'page',
    GpuPage,
    {
      download: 'settings.gpu.download.button',
      rocm: 'settings.gpu.downloadRocm.button',
      remove: 'settings.gpu.remove.button',
      cpu: 'settings.gpu.switchToCpu.button',
    },
  ],
  [
    'settings',
    GpuAcceleration,
    {
      download: 'Download CUDA Backend',
      rocm: 'Download AMD ROCm Backend',
      remove: 'Remove CUDA Backend',
      cpu: 'Switch to CPU Backend',
    },
  ],
] as const) {
  test(`${name}: CPU and native GPU status`, async () => {
    let s = await setup(Component, { tauri: false });
    expect(s.button(labels.download)).toBeDefined();
    expect(s.button(labels.rocm)).toBeDefined();
    s.finish();
    s = await setup(Component, {
      health: {
        gpu_available: true,
        gpu_type: 'MPS (Apple M4)',
        backend_variant: 'mps',
        vram_used_mb: 12.6,
      },
    });
    expect(s.text()).toContain('Apple M4');
    expect(s.text()).toContain('13');
    expect(s.button(labels.download)).toBeUndefined();
    s.finish();
    s = await setup(Component, { health: null });
    expect(s.view.toJSON()).toBeNull();
    s.finish();
  });
  test(`${name}: download errors and retry`, async () => {
    const s = await setup(Component);
    downloadCuda.mockImplementationOnce(async () => {
      throw new Error('offline');
    });
    await s.click(labels.download);
    expect(s.text()).toContain('offline');
    downloadCuda.mockImplementationOnce(async () => {
      throw new Error('already downloaded');
    });
    await s.click(labels.download);
    expect(s.text()).not.toContain('offline');
    expect(getCuda).toHaveBeenCalled();
    downloadRocm.mockImplementationOnce(async () => {
      throw new Error('AMD offline');
    });
    await s.click(labels.rocm);
    expect(s.text()).toContain('AMD offline');
    await s.click(labels.rocm);
    expect(downloadRocm).toHaveBeenCalledTimes(2);
    s.finish();
  });
  test(`${name}: progress, errors and stream cleanup`, async () => {
    const s = await setup(Component, {
      cuda: { available: false, downloading: true },
      rocm: { available: false, downloading: true },
    });
    const c = FakeEventSource.instances.find((x) => x.url.endsWith('cuda-progress'))!,
      r = FakeEventSource.instances.find((x) => x.url.endsWith('rocm-progress'))!;
    expect(c).toBeDefined();
    expect(r).toBeDefined();
    act(() => {
      c.send({
        status: 'downloading',
        filename: 'cuda.zip',
        current: 1024,
        total: 2048,
        progress: 50,
      });
      r.send({
        status: 'downloading',
        filename: 'rocm.zip',
        current: 512,
        total: 1024,
        progress: 50,
      });
    });
    expect(s.text()).toContain('cuda.zip');
    expect(s.text()).toContain('rocm.zip');
    expect(s.text()).toContain('1.0 KB');
    act(() => c.send({ status: 'error', error: 'stream failed' }));
    expect(c.closed).toBe(true);
    expect(s.text()).toContain('stream failed');
    expect(getCuda).toHaveBeenCalled();
    s.finish();
    expect(r.closed).toBe(true);
  });
  test(`${name}: remove failure and retry`, async () => {
    const s = await setup(Component, { cuda: { available: true, downloading: false } });
    deleteCuda.mockImplementationOnce(async () => {
      throw new Error('delete failed');
    });
    await s.click(labels.remove);
    expect(s.text()).toContain('delete failed');
    await s.click(labels.remove);
    expect(deleteCuda).toHaveBeenCalledTimes(2);
    expect(s.text()).not.toContain('delete failed');
    s.finish();
  });
  test(`${name}: active backend CPU switch failure and retry`, async () => {
    const s = await setup(Component, {
      health: { gpu_available: true, gpu_type: 'CUDA (NVIDIA)', backend_variant: 'cuda' },
    });
    override.mockImplementationOnce(async () => {
      throw new Error('override failed');
    });
    await s.click(labels.cpu);
    expect(s.text()).toContain('override failed');
    expect(getCuda).toHaveBeenCalled();
    await s.click(labels.cpu);
    expect(override).toHaveBeenCalledWith('cpu');
    expect(restart).toHaveBeenCalled();
    s.finish();
  });
}

test('page: available backends switch and remove through their respective boundaries', async () => {
  const s = await setup(GpuPage, {
    cuda: { available: true, downloading: false },
    rocm: { available: true, downloading: false },
  });
  await s.click('settings.gpu.removeRocm.button');
  expect(deleteRocm).toHaveBeenCalled();
  override.mockImplementationOnce(async () => {
    throw new Error('ROCm switch failed');
  });
  await s.click('settings.gpu.switchToRocm.button');
  expect(s.text()).toContain('ROCm switch failed');
  override.mockImplementationOnce(async () => {
    throw new Error('CUDA switch failed');
  });
  await s.click('settings.gpu.switchToCuda.button');
  expect(s.text()).toContain('CUDA switch failed');
  s.finish();
});

test('settings: available ROCm removal and restart failures can be retried', async () => {
  const s = await setup(GpuAcceleration, {
    cuda: { available: true, downloading: false },
    rocm: { available: true, downloading: false },
  });
  deleteRocm.mockImplementationOnce(async () => {
    throw new Error('remove AMD failed');
  });
  await s.click('Remove ROCm Backend');
  expect(s.text()).toContain('remove AMD failed');
  await s.click('Remove ROCm Backend');
  expect(deleteRocm).toHaveBeenCalledTimes(2);
  restart.mockImplementationOnce(async () => {
    throw new Error('restart failed');
  });
  await s.click('Switch to ROCm Backend');
  expect(s.text()).toContain('restart failed');
  await s.click('Switch to CUDA Backend');
  expect(restart).toHaveBeenCalledTimes(2);
  s.finish();
});

for (const [name, Component, cpu] of [
  ['page', GpuPage, 'settings.gpu.switchToCpu.button'],
  ['settings', GpuAcceleration, 'Switch to CPU Backend'],
] as const) {
  test(`${name}: active ROCm switch failure and stream completion`, async () => {
    let s = await setup(Component, {
      health: { gpu_available: true, gpu_type: 'ROCm (Radeon)', backend_variant: 'rocm' },
    });
    override.mockImplementationOnce(async () => {
      throw new Error('CPU switch failed');
    });
    await s.click(cpu);
    expect(s.text()).toContain('CPU switch failed');
    expect(getRocm).toHaveBeenCalled();
    s.finish();
    s = await setup(Component, { rocm: { available: false, downloading: true } });
    const stream = FakeEventSource.instances.find((x) => x.url.endsWith('rocm-progress'))!;
    act(() => stream.send({ status: 'complete' }));
    expect(stream.closed).toBe(true);
    expect(getRocm).toHaveBeenCalled();
    s.finish();
  });
}
