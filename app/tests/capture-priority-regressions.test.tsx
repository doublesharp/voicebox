import { afterAll, afterEach, expect, mock, test } from 'bun:test';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, create, type ReactTestRenderer } from 'react-test-renderer';
import type { CaptureResponse } from '../src/lib/api/types';

const deferred = <T,>() => {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((r) => {
    resolve = r;
  });
  return { promise, resolve };
};
const refinements = new Map<string, ReturnType<typeof deferred<CaptureResponse>>>();
const createCapture = mock(
  async (file: File) =>
    ({
      id: file.name,
      transcript_raw: file.name,
      transcript_refined: null,
      auto_refine: true,
      allow_auto_paste: file.name === 'first',
    }) as CaptureResponse,
);
mock.module('../src/lib/api/client', () => ({
  apiClient: {
    createCapture,
    refineCapture: (id: string) => refinements.get(id)!.promise,
  },
}));
mock.module('@tauri-apps/api/event', () => ({ emit: async () => {} }));
mock.module('../src/lib/hooks/useAudioRecording', () => ({
  useAudioRecording: () => ({
    isRecording: false,
    duration: 0,
    startRecording() {},
    stopRecording() {},
  }),
}));
const { useCaptureRecordingSession } = await import('../src/lib/hooks/useCaptureRecordingSession');
const previousWindow = Object.getOwnPropertyDescriptor(globalThis, 'window');
Object.defineProperty(globalThis, 'window', { configurable: true, value: globalThis });
let tree: ReactTestRenderer;
let client: QueryClient;
afterEach(() => {
  act(() => tree?.unmount());
  client?.clear();
  refinements.clear();
});
afterAll(() => {
  mock.restore();
  if (previousWindow) Object.defineProperty(globalThis, 'window', previousWindow);
  else Reflect.deleteProperty(globalThis, 'window');
});

test('overlapping refinements retain the auto-paste setting of each capture', async () => {
  const first = deferred<CaptureResponse>();
  const second = deferred<CaptureResponse>();
  refinements.set('first', first);
  refinements.set('second', second);
  const delivered = mock(() => {});
  let session!: ReturnType<typeof useCaptureRecordingSession>;
  client = new QueryClient({ defaultOptions: { mutations: { retry: false } } });
  function Harness() {
    session = useCaptureRecordingSession({ onFinalText: delivered });
    return null;
  }
  act(() => {
    tree = create(
      <QueryClientProvider client={client}>
        <Harness />
      </QueryClientProvider>,
    );
  });
  await act(async () => {
    session.uploadFile(new File(['x'], 'first'), 'file');
    await new Promise((r) => setTimeout(r, 10));
  });
  await act(async () => {
    session.uploadFile(new File(['x'], 'second'), 'file');
    await new Promise((r) => setTimeout(r, 10));
  });
  const row = {
    id: 'first',
    transcript_raw: 'raw',
    transcript_refined: 'first result',
  } as CaptureResponse;
  await act(async () => {
    first.resolve(row);
    await new Promise((r) => setTimeout(r, 10));
  });
  expect(delivered).toHaveBeenCalledWith('first result', row, true);
  const row2 = {
    id: 'second',
    transcript_raw: 'raw',
    transcript_refined: 'second result',
  } as CaptureResponse;
  await act(async () => {
    second.resolve(row2);
    await new Promise((r) => setTimeout(r, 10));
  });
  expect(delivered).toHaveBeenCalledWith('second result', row2, false);
});

test('starting a recording cancels an earlier error notice timer', async () => {
  createCapture.mockImplementationOnce(async () => {
    throw new Error('Could not decode uploaded audio');
  });
  let session!: ReturnType<typeof useCaptureRecordingSession>;
  client = new QueryClient({ defaultOptions: { mutations: { retry: false } } });
  function Harness() {
    session = useCaptureRecordingSession();
    return null;
  }
  act(() => {
    tree = create(
      <QueryClientProvider client={client}>
        <Harness />
      </QueryClientProvider>,
    );
  });
  await act(async () => {
    session.uploadFile(new File(['x'], 'bad'), 'dictation');
    await new Promise((r) => setTimeout(r, 10));
  });
  expect(session.pillState).toBe('error');
  act(() => session.startRecording());
  expect(session.pillState).toBe('recording');
  await act(async () => {
    await new Promise((r) => setTimeout(r, 2050));
  });
  expect(session.pillState).toBe('recording');
});
