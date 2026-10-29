import { afterEach, expect, mock, test } from 'bun:test';
import { act, create, type ReactTestRenderer } from 'react-test-renderer';
import { StoryCopy } from '../src/features/studio/story-copy';

class MemoryStorage {
  values = new Map<string, string>();
  failWrites = false;
  get length() {
    return this.values.size;
  }
  key(index: number) {
    return [...this.values.keys()][index] ?? null;
  }
  getItem(key: string) {
    return this.values.get(key) ?? null;
  }
  setItem(key: string, value: string) {
    if (this.failWrites) throw new Error('Storage unavailable');
    this.values.set(key, value);
  }
  removeItem(key: string) {
    this.values.delete(key);
  }
}

const project = (id = 'studio-copy') => ({
  id,
  name: 'My Story',
  document: { schema_version: 1, passages: [], tracks: [], clips: [] },
});
let storage = new MemoryStorage();
let endpoint = 'http://server';
let destination: unknown = null;
let navigateFailure = false;
let renderer: ReactTestRenderer | undefined;
const oldFetch = globalThis.fetch;
const oldStorage = globalThis.localStorage;
const oldWindow = globalThis.window;
mock.module('@tanstack/react-router', () => ({
  useNavigate: () => async (value: unknown) => {
    if (navigateFailure) throw new Error('Navigation failed');
    destination = value;
  },
}));
mock.module('../src/stores/serverStore', () => ({
  useServerStore: (select: (state: { serverUrl: string }) => string) =>
    select({ serverUrl: endpoint }),
}));
mock.module('../src/components/ui/button', () => ({
  Button: (props: Record<string, unknown>) => <button {...props} />,
}));
const { CreateStudioCopyButton } = await import(
  '../src/components/StoriesTab/CreateStudioCopyButton'
);

afterEach(async () => {
  await act(async () => renderer?.unmount());
  renderer = undefined;
  globalThis.fetch = oldFetch;
  globalThis.localStorage = oldStorage;
  globalThis.window = oldWindow;
  endpoint = 'http://server';
  destination = null;
  navigateFailure = false;
  storage = new MemoryStorage();
});
async function mount(storyId = 'story') {
  globalThis.localStorage = storage as unknown as Storage;
  globalThis.window = { addEventListener() {}, removeEventListener() {} } as unknown as Window &
    typeof globalThis;
  await act(async () => {
    renderer = create(<CreateStudioCopyButton storyId={storyId} />);
  });
}
async function click() {
  await act(async () => {
    renderer!.root.findByType('button').props.onClick();
  });
}
function label() {
  return renderer!.root
    .findByType('button')
    .props.children.flat()
    .filter((value: unknown) => typeof value === 'string')
    .join('');
}

test('lost response and component restart replay exact body then open one Studio project', async () => {
  const requests: Array<{ url: string; body: string }> = [];
  let loseResponse = true;
  globalThis.fetch = (async (url, init) => {
    requests.push({ url: String(url), body: String(init?.body) });
    if (loseResponse) throw new Error('Connection closed after commit');
    return Response.json(project());
  }) as typeof fetch;
  await mount('story/id');
  expect(label()).toBe('Create studio copy');
  await click();
  expect(renderer!.root.findByProps({ role: 'alert' }).props.children).toContain(
    'Connection closed',
  );
  expect(destination).toBeNull();
  expect(storage.length).toBe(1);
  await act(async () => renderer!.unmount());
  loseResponse = false;
  await mount('story/id');
  expect(label()).toBe('Retry studio copy');
  await click();
  expect(requests[1]).toEqual(requests[0]);
  expect(requests[0].url).toBe('http://server/studio/v1/imports/story/story%2Fid');
  expect(destination).toEqual({
    to: '/studio/projects/$projectId/$workspace',
    params: { projectId: 'studio-copy', workspace: 'edit' },
  });
  expect(storage.length).toBe(0);
});

test('acknowledged destination survives navigation failure and opens without another POST', async () => {
  let posts = 0;
  globalThis.fetch = (async () => {
    posts++;
    return Response.json(project());
  }) as typeof fetch;
  navigateFailure = true;
  await mount();
  await click();
  expect(label()).toBe('Open studio copy');
  expect(storage.length).toBe(1);
  navigateFailure = false;
  await click();
  expect(posts).toBe(1);
  expect(storage.length).toBe(0);
  expect(destination).not.toBeNull();
});

test('storage failure prevents network mutation and is visible', async () => {
  let posts = 0;
  globalThis.fetch = (async () => {
    posts++;
    return Response.json(project());
  }) as typeof fetch;
  storage.failWrites = true;
  await mount();
  await click();
  expect(posts).toBe(0);
  expect(renderer!.root.findByProps({ role: 'alert' }).props.children).toContain(
    'Storage unavailable',
  );
});

test('unmount and server change fence navigation while the original server retains its receipt', async () => {
  let release!: (response: Response) => void;
  globalThis.fetch = (() =>
    new Promise<Response>((resolve) => {
      release = resolve;
    })) as typeof fetch;
  await mount();
  await click();
  await act(async () => renderer!.unmount());
  endpoint = 'http://other';
  await mount();
  await act(async () => {
    release(Response.json(project('original-server-copy')));
  });
  expect(destination).toBeNull();
  expect(label()).toBe('Create studio copy');
  expect(new StoryCopy(storage, 'http://server', 'story').pending()?.project_id).toBe(
    'original-server-copy',
  );
});

test('malformed successful acknowledgement retains original retry identity', async () => {
  const memory = new MemoryStorage();
  const requests: string[] = [];
  const copy = new StoryCopy(memory, 'http://server/', 'story', (async (_url, init) => {
    requests.push(String(init?.body));
    return Response.json({ id: 'incomplete' });
  }) as typeof fetch);
  await expect(copy.copy(null)).rejects.toThrow('acknowledgement was incomplete');
  const key = copy.pending()!.idempotency_key;
  await expect(copy.copy(key)).rejects.toThrow('acknowledgement was incomplete');
  expect(requests[1]).toBe(requests[0]);
  expect(memory.length).toBe(1);
});

test('different Story and server cannot replay each other; corrupt journals never submit', async () => {
  const memory = new MemoryStorage();
  const copy = new StoryCopy(memory, 'http://server', 'story', (async () => {
    throw new Error('lost');
  }) as typeof fetch);
  await expect(copy.copy(null)).rejects.toThrow('lost');
  expect(new StoryCopy(memory, 'http://other', 'story').pending()).toBeNull();
  expect(new StoryCopy(memory, 'http://server', 'other-story').pending()).toBeNull();
  const key = memory.key(0)!;
  memory.setItem(key, '{broken');
  let called = false;
  const restarted = new StoryCopy(memory, 'http://server', 'story', (async () => {
    called = true;
    return Response.json(project());
  }) as typeof fetch);
  await expect(restarted.copy(null)).rejects.toThrow();
  expect(called).toBe(false);
  expect(memory.getItem(key)).toBe('{broken');
});
