import { afterAll, afterEach, expect, mock, test } from 'bun:test';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { createElement } from 'react';
import { act, create, type ReactTestRenderer } from 'react-test-renderer';
import type { CaptureSource } from '../src/lib/api/types';

mock.module('../src/lib/api/client', () => ({
  apiClient: {
    createCapture: async () => {
      throw new Error('Could not decode uploaded audio');
    },
  },
}));
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
afterAll(() => {
  mock.restore();
  if (previousWindow) Object.defineProperty(globalThis, 'window', previousWindow);
  else Reflect.deleteProperty(globalThis, 'window');
});
let tree: ReactTestRenderer | undefined;
let client: QueryClient;
afterEach(() => {
  act(() => tree?.unmount());
  tree = undefined;
  client?.clear();
});
for (const source of ['file', 'recording', 'dictation'] as CaptureSource[]) {
  test(`capture ${source} shows the appropriate decode error`, async () => {
    client = new QueryClient();
    let session!: ReturnType<typeof useCaptureRecordingSession>;
    function Harness() {
      session = useCaptureRecordingSession();
      return null;
    }
    act(() => {
      tree = create(createElement(QueryClientProvider, { client }, createElement(Harness)));
    });
    await act(async () => {
      session.uploadFile(new File(['audio'], 'upload.webm'), source);
      await new Promise((resolve) => setTimeout(resolve, 10));
    });
    expect(session.pillState).toBe('error');
    expect(session.errorMessage).toBe(
      source === 'dictation' ? 'Recording too short, canceled' : 'Could not decode uploaded audio',
    );
  });
}
