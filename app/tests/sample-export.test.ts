import { afterEach, expect, mock, test } from 'bun:test';

mock.module('../src/stores/serverStore', () => ({
  useServerStore: { getState: () => ({ serverUrl: 'http://voicebox.test:17493' }) },
}));

const { apiClient } = await import('../src/lib/api/client');
const originalFetch = globalThis.fetch;
afterEach(() => {
  globalThis.fetch = originalFetch;
});

test('sample export requests MP3 and returns the audio bytes', async () => {
  const requests: string[] = [];
  globalThis.fetch = mock(async (url: string | URL | Request) => {
    requests.push(String(url));
    return new Response('encoded audio', { headers: { 'Content-Type': 'audio/mpeg' } });
  }) as typeof fetch;

  const blob = await apiClient.exportSampleAudio('sample-1');
  expect(requests).toEqual(['http://voicebox.test:17493/samples/sample-1/export-audio?format=mp3']);
  expect(blob.type).toBe('audio/mpeg');
  expect(await blob.text()).toBe('encoded audio');
});

test('lossless export selects WAV and safely encodes the sample ID', async () => {
  const requests: string[] = [];
  globalThis.fetch = mock(async (url: string | URL | Request) => {
    requests.push(String(url));
    return new Response('wave audio');
  }) as typeof fetch;

  await apiClient.exportSampleAudio('sample/1?', 'wav');
  expect(requests).toEqual([
    'http://voicebox.test:17493/samples/sample%2F1%3F/export-audio?format=wav',
  ]);
});

test('encoder errors preserve the actionable backend message', async () => {
  globalThis.fetch = mock(async () =>
    Response.json(
      { detail: 'Install ffmpeg and restart Voicebox, or export as WAV.' },
      { status: 503 },
    ),
  ) as typeof fetch;
  await expect(apiClient.exportSampleAudio('sample-1')).rejects.toThrow(
    'Install ffmpeg and restart Voicebox, or export as WAV.',
  );
});

test('non-JSON server errors fall back to the HTTP status text', async () => {
  globalThis.fetch = mock(
    async () =>
      new Response('unavailable', {
        status: 502,
        statusText: 'Bad Gateway',
      }),
  ) as typeof fetch;
  await expect(apiClient.exportSampleAudio('sample-1')).rejects.toThrow('Bad Gateway');
});

const savedFiles: { filename: string; blob: Blob; filters: unknown }[] = [];
mock.module('../src/platform/PlatformContext', () => ({
  usePlatform: () => ({
    filesystem: {
      saveFile: async (filename: string, blob: Blob, filters: unknown) => {
        savedFiles.push({ filename, blob, filters });
      },
    },
  }),
}));
const { QueryClient, QueryClientProvider } = await import('@tanstack/react-query');
const { createElement } = await import('react');
const { renderToString } = await import('react-dom/server');
const { useExportSampleAudio } = await import('../src/lib/hooks/useProfiles');

function exportMutation() {
  let result: ReturnType<typeof useExportSampleAudio> | undefined;
  function Harness() {
    result = useExportSampleAudio();
    return null;
  }
  const client = new QueryClient({ defaultOptions: { mutations: { retry: false } } });
  renderToString(createElement(QueryClientProvider, { client }, createElement(Harness)));
  if (!result) throw new Error('Export hook did not render');
  return { mutation: result, client };
}

test('sample export hands audio and a descriptive MP3 filename to the platform saver', async () => {
  savedFiles.length = 0;
  globalThis.fetch = mock(async () => new Response('MP3 bytes')) as typeof fetch;
  const { mutation, client } = exportMutation();
  try {
    await mutation.mutateAsync({
      sampleId: 'abcdefgh-1234',
      referenceText: 'Hello / Justin!',
      format: 'mp3',
    });
    expect(savedFiles).toHaveLength(1);
    expect(savedFiles[0].filename).toBe('sample-hello---justin--abcdefgh.mp3');
    expect(savedFiles[0].filters).toEqual([{ name: 'Audio File', extensions: ['mp3'] }]);
    expect(await savedFiles[0].blob.text()).toBe('MP3 bytes');
  } finally {
    client.clear();
  }
});

test('failed conversion never opens a save dialog', async () => {
  savedFiles.length = 0;
  globalThis.fetch = mock(async () =>
    Response.json({ detail: 'Install ffmpeg' }, { status: 503 }),
  ) as typeof fetch;
  const { mutation, client } = exportMutation();
  try {
    await expect(
      mutation.mutateAsync({ sampleId: 'sample-1', referenceText: 'Hello', format: 'mp3' }),
    ).rejects.toThrow('Install ffmpeg');
    expect(savedFiles).toHaveLength(0);
  } finally {
    client.clear();
  }
});
