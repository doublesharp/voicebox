import { afterEach, expect, mock, test } from 'bun:test';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, create, type ReactTestRenderer } from 'react-test-renderer';
import { apiClient } from '../src/lib/api/client';
import { useSpeechPreview } from '../src/lib/audio/useSpeechPreview';
import { PlatformProvider } from '../src/platform/PlatformContext';
import type { Platform } from '../src/platform/types';
import { usePlayerStore } from '../src/stores/playerStore';

let view: ReactTestRenderer | undefined;
let hook!: ReturnType<typeof useSpeechPreview>;
let client: QueryClient;
let profile = 'voice';
let signal: AbortSignal | undefined;
let requestCount = 0;
let errors: string[] = [];
const originalRoutes = apiClient.getProfileChannels;
const originalChannels = apiClient.listChannels;
const originalPreview = apiClient.previewSpeech;
const stops = mock(async (_id?: string) => {});
const starts = mock(async (_rate: number, _ids: string[]) => ({
  session_id: 'session',
  duration: 0,
  position: 0,
}));
const platform = {
  metadata: { isTauri: true },
  audio: {
    stopPlayback: stops,
    startPcmStream: starts,
    appendPcmStream: async () => {},
    finishPcmStream: async () => {},
    subscribeToPlayback: async () => () => {},
  },
} as unknown as Platform;
const data = { profile_id: 'voice', text: 'Exact draft', engine: 'qwen_custom_voice' as const };
function header() {
  const bytes = new Uint8Array(48);
  const v = new DataView(bytes.buffer);
  bytes.set(new TextEncoder().encode('RIFF'), 0);
  v.setUint32(4, 0xffffffff, true);
  bytes.set(new TextEncoder().encode('WAVEfmt '), 8);
  v.setUint32(16, 16, true);
  v.setUint16(20, 1, true);
  v.setUint16(22, 1, true);
  v.setUint32(24, 24000, true);
  v.setUint32(28, 48000, true);
  v.setUint16(32, 2, true);
  v.setUint16(34, 16, true);
  bytes.set(new TextEncoder().encode('data'), 36);
  v.setUint32(40, 0xffffffff, true);
  return bytes;
}
function Probe() {
  hook = useSpeechPreview(profile, (error) => errors.push(error));
  return null;
}
function tree() {
  return (
    <QueryClientProvider client={client}>
      <PlatformProvider platform={platform}>
        <Probe />
      </PlatformProvider>
    </QueryClientProvider>
  );
}
async function flush() {
  await act(async () => {
    for (let i = 0; i < 30; i++) await Promise.resolve();
  });
}
async function mount() {
  stops.mockClear();
  starts.mockClear();
  requestCount = 0;
  errors = [];
  signal = undefined;
  profile = 'voice';
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  apiClient.getProfileChannels = async () => ({ channel_ids: [] });
  apiClient.listChannels = async () => [];
  apiClient.previewSpeech = async (_data, abort) => {
    requestCount++;
    signal = abort;
    return new Response(
      new ReadableStream({
        start(controller) {
          controller.enqueue(header());
        },
      }),
    );
  };
  await act(async () => {
    view = create(tree());
  });
}
afterEach(async () => {
  await act(async () => {
    view?.unmount();
  });
  view = undefined;
  apiClient.getProfileChannels = originalRoutes;
  apiClient.listChannels = originalChannels;
  apiClient.previewSpeech = originalPreview;
  client?.clear();
  usePlayerStore.getState().reset();
});

test('preview uses current voice routing; route edits and selected saved audio stop response and device', async () => {
  await mount();
  apiClient.getProfileChannels = async () => ({ channel_ids: ['assigned'] });
  apiClient.listChannels = async () => [
    {
      id: 'assigned',
      device_ids: ['device-a'],
      is_default: false,
      name: 'Speakers',
      created_at: '',
    },
  ];
  let playing!: Promise<void>;
  await act(async () => {
    playing = hook.start(data);
  });
  await flush();
  expect(starts).toHaveBeenCalledWith(24000, ['device-a']);
  expect(hook.active).toBe(true);
  await act(async () => {
    client.setQueryData(['channels'], []);
  });
  await playing;
  expect(signal!.aborted).toBe(true);
  expect(hook.active).toBe(false);
  await act(async () => {
    playing = hook.start(data);
  });
  await flush();
  await act(async () => {
    usePlayerStore.getState().setAudio('/saved', 'saved', 'voice');
  });
  await playing;
  expect(signal!.aborted).toBe(true);
  expect(stops.mock.calls.some(([id]) => id === 'session')).toBe(true);
});

test('stopping or changing voice during pending routing cannot later start the old voice', async () => {
  for (const changeVoice of [false, true]) {
    await mount();
    let resolve!: (value: { channel_ids: string[] }) => void;
    apiClient.getProfileChannels = () =>
      new Promise((done) => {
        resolve = done;
      });
    let playing!: Promise<void>;
    await act(async () => {
      playing = hook.start(data);
    });
    if (changeVoice) {
      profile = 'other-voice';
      await act(async () => {
        view!.update(tree());
      });
    } else
      await act(async () => {
        await hook.stop();
      });
    await act(async () => {
      resolve({ channel_ids: [] });
      await playing;
    });
    expect(requestCount).toBe(0);
    expect(starts).not.toHaveBeenCalled();
    await act(async () => {
      view!.unmount();
    });
    view = undefined;
    client.clear();
  }
});

test('unmount aborts network and stops native; unavailable assigned channel fails without default playback', async () => {
  await mount();
  let playing!: Promise<void>;
  await act(async () => {
    playing = hook.start(data);
  });
  await flush();
  await act(async () => {
    view!.unmount();
  });
  view = undefined;
  await playing;
  expect(signal!.aborted).toBe(true);
  await mount();
  apiClient.getProfileChannels = async () => ({ channel_ids: ['missing'] });
  await act(async () => {
    await hook.start(data);
  });
  expect(requestCount).toBe(0);
  expect(starts).not.toHaveBeenCalled();
  expect(errors).toEqual(['A voice output channel is unavailable']);
});

test('a backend switch revokes pending routing before any request reaches the new backend', async () => {
  const { useServerStore } = await import('../src/stores/serverStore');
  const originalServer = useServerStore.getState().serverUrl;
  await mount();
  let resolve!: (value: { channel_ids: string[] }) => void;
  let channelRequests = 0;
  apiClient.getProfileChannels = () =>
    new Promise((done) => {
      resolve = done;
    });
  apiClient.listChannels = async () => {
    channelRequests++;
    return [];
  };
  let playing!: Promise<void>;
  try {
    await act(async () => {
      playing = hook.start(data);
    });
    await act(async () => {
      useServerStore.getState().setServerUrl('http://new-backend.invalid');
    });
    await act(async () => {
      resolve({ channel_ids: ['old-device'] });
      await playing;
    });
    expect(channelRequests).toBe(0);
    expect(requestCount).toBe(0);
    expect(starts).not.toHaveBeenCalled();
    expect(hook.active).toBe(false);
  } finally {
    await act(async () => {
      useServerStore.getState().setServerUrl(originalServer);
    });
  }
});

test('a backend switch aborts an active response and its native device session', async () => {
  const { useServerStore } = await import('../src/stores/serverStore');
  const originalServer = useServerStore.getState().serverUrl;
  await mount();
  let playing!: Promise<void>;
  try {
    await act(async () => {
      playing = hook.start(data);
    });
    await flush();
    expect(signal!.aborted).toBe(false);
    await act(async () => {
      useServerStore.getState().setServerUrl('http://another-backend.invalid');
      await playing;
    });
    expect(signal!.aborted).toBe(true);
    expect(stops.mock.calls.some(([id]) => id === 'session')).toBe(true);
    expect(hook.active).toBe(false);
  } finally {
    await act(async () => {
      useServerStore.getState().setServerUrl(originalServer);
    });
  }
});
