import { expect, test } from 'bun:test';
import { CleanupClient } from '../src/features/studio/cleanup-client';
import { StudioClient } from '../src/features/studio/client';
import { ReviewClient } from '../src/features/studio/review-client';
import { StoryCopy } from '../src/features/studio/story-copy';

function browserFetch(body: unknown): typeof fetch {
  return async function (this: unknown) {
    if (this !== undefined && this !== globalThis) throw new TypeError('Illegal invocation');
    return Response.json(body);
  } as typeof fetch;
}

test('Studio requests preserve the native fetch receiver contract', async () => {
  const client = new StudioClient('http://server', browserFetch({ items: [] }));
  expect(await client.projects()).toEqual({ items: [] });
});

test('Studio downloads preserve the native fetch receiver contract', async () => {
  const client = new StudioClient('http://server', browserFetch('audio'));
  expect(await (await client.download('http://server/audio')).text()).toBe('"audio"');
});

test('Review requests preserve the native fetch receiver contract', async () => {
  const review = { revision_id: 'r', version: 0, note: '', checklist: [], updated_at: null };
  const client = new ReviewClient('http://server', browserFetch({ review }));
  expect(await client.get('p', 'r')).toEqual({ review });
});

test('Storage requests preserve the native fetch receiver contract', async () => {
  const preview = {
    project_id: 'p',
    preview_token: 'a'.repeat(64),
    candidates: [],
    protected_count: 0,
    trash: [],
    policy: 'Retain recoverable audio',
  };
  const client = new CleanupClient('http://server', browserFetch(preview));
  expect(await client.preview('p')).toEqual(preview);
});

test('Story copies preserve the native fetch receiver contract', async () => {
  const values = new Map<string, string>();
  const storage = {
    get length() {
      return values.size;
    },
    key: (index: number) => [...values.keys()][index] ?? null,
    getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => {
      values.set(key, value);
    },
    removeItem: (key: string) => {
      values.delete(key);
    },
  };
  const copy = new StoryCopy(
    storage,
    'http://server',
    'story',
    browserFetch({
      id: 'p',
      name: 'Copied Story',
      document: { schema_version: 1, passages: [], tracks: [], clips: [] },
    }),
  );
  expect(await copy.copy(null)).toBe('p');
});
