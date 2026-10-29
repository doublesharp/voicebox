import { afterEach, expect, test } from 'bun:test';
import { apiClient } from '../src/lib/api/client';
import { useServerStore } from '../src/stores/serverStore';

const originalFetch = globalThis.fetch;
const originalUrl = useServerStore.getState().serverUrl;
afterEach(() => {
  globalThis.fetch = originalFetch;
  useServerStore.setState({ serverUrl: originalUrl });
});

test('JSON endpoint contracts use the current server and serialize mutations', async () => {
  useServerStore.setState({ serverUrl: 'http://server.test' });
  const cases: Array<[() => Promise<unknown>, string, string, unknown?]> = [
    [() => apiClient.listProfiles(), '/profiles', 'GET'],
    [() => apiClient.getProfile('p1'), '/profiles/p1', 'GET'],
    [
      () => apiClient.createProfile({ name: 'Voice' } as never),
      '/profiles',
      'POST',
      { name: 'Voice' },
    ],
    [
      () => apiClient.updateProfileEffects('p1', null),
      '/profiles/p1/effects',
      'PUT',
      { effects_chain: null },
    ],
    [() => apiClient.listStories(), '/stories', 'GET'],
    [
      () => apiClient.createStory({ name: 'Story' } as never),
      '/stories',
      'POST',
      { name: 'Story' },
    ],
    [
      () => apiClient.moveStoryItem('s', 'i', { start_time: 2 } as never),
      '/stories/s/items/i/move',
      'PUT',
      { start_time: 2 },
    ],
    [
      () => apiClient.deleteMCPBinding('client / one'),
      '/mcp/bindings/client%20%2F%20one',
      'DELETE',
    ],
    [
      () => apiClient.setChannelVoices('c', ['p']),
      '/channels/c/voices',
      'PUT',
      { profile_ids: ['p'] },
    ],
    [
      () => apiClient.updateGenerationSettings({ speed: 1 } as never),
      '/settings/generation',
      'PUT',
      { speed: 1 },
    ],
  ];
  for (const [call, path, method, body] of cases) {
    let request: [string, RequestInit | undefined] | undefined;
    globalThis.fetch = (async (url, init) => {
      request = [String(url), init];
      return Response.json({ result: 'ok' });
    }) as typeof fetch;
    expect(await call()).toEqual({ result: 'ok' });
    expect(request?.[0]).toBe(`http://server.test${path}`);
    expect(request?.[1]?.method ?? 'GET').toBe(method);
    expect(request?.[1]?.headers).toEqual({ 'Content-Type': 'application/json' });
    expect(request?.[1]?.body).toBe(body === undefined ? undefined : JSON.stringify(body));
  }
});

test('reports structured and malformed error payloads without masking the HTTP failure', async () => {
  const cases: Array<[string, string]> = [
    [JSON.stringify({ detail: 'denied' }), 'denied'],
    [JSON.stringify({ detail: [{ msg: 'first' }, { message: 'second' }] }), 'first; second'],
    [JSON.stringify({ detail: [null, 'bad'] }), 'null; "bad"'],
    [JSON.stringify({ detail: { message: 'invalid' } }), 'invalid'],
    ['not json', 'Unavailable'],
  ];
  for (const [body, message] of cases) {
    globalThis.fetch = (async () =>
      new Response(body, { status: 503, statusText: 'Unavailable' })) as typeof fetch;
    await expect(apiClient.getHealth()).rejects.toThrow(message);
  }
});

test('successful empty DELETE responses are accepted', async () => {
  globalThis.fetch = (async () => new Response(null, { status: 204 })) as typeof fetch;
  await expect(apiClient.deleteStory('s')).resolves.toBeUndefined();
});

test('transport errors propagate and multipart upload leaves content type to fetch', async () => {
  const failure = new Error('offline');
  globalThis.fetch = (async () => {
    throw failure;
  }) as typeof fetch;
  await expect(apiClient.listProfiles()).rejects.toBe(failure);
  let request: RequestInit | undefined;
  globalThis.fetch = (async (_url, init) => {
    request = init;
    return Response.json({ id: 'sample' });
  }) as typeof fetch;
  const file = new File(['audio'], 'sample.wav', { type: 'audio/wav' });
  expect(await apiClient.addProfileSample('p', file, 'words')).toEqual({ id: 'sample' });
  expect(request?.headers).toBeUndefined();
  expect((request?.body as FormData).get('reference_text')).toBe('words');
  expect((request?.body as FormData).get('file')).toBeInstanceOf(File);
});

test('story export returns binary and includes chapter option only for non-wav', async () => {
  const urls: string[] = [];
  globalThis.fetch = (async (url) => {
    urls.push(String(url));
    return new Response('audio');
  }) as typeof fetch;
  expect(await (await apiClient.exportStoryAudio('s')).text()).toBe('audio');
  expect(await (await apiClient.exportStoryAudio('s', 'mp3')).text()).toBe('audio');
  expect(urls[0]).toEndWith('/stories/s/export-audio?format=wav');
  expect(urls[1]).toEndWith('/stories/s/export-audio?format=mp3&chapters=auto');
});

test('null and primitive HTTP error bodies retain the HTTP fallback', async () => {
  for (const payload of [null, 42, 'denied', false]) {
    globalThis.fetch = (async () => Response.json(payload, { status: 503 })) as typeof fetch;
    await expect(apiClient.getHealth()).rejects.toThrow('HTTP error! status: 503');
  }
});
