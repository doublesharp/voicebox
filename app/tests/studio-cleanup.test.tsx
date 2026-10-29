import { afterEach, expect, test } from 'bun:test';
import { act, create, type ReactTestRenderer } from 'react-test-renderer';
import {
  type CleanupBody,
  CleanupClient,
  type CleanupPreview,
  type CleanupReceipt,
  CleanupSession,
  type CleanupTransport,
} from '../src/features/studio/cleanup-client';
import { StorageCleanupPanel } from '../src/features/studio/StorageCleanupDialog';
import type { RecoveryStorage } from '../src/features/studio/session';

const project = '11111111-1111-4111-8111-111111111111';
const asset = '22222222-2222-4222-8222-222222222222';
const receipt = '33333333-3333-4333-8333-333333333333';
const body: CleanupBody = {
  operation_id: 'original-operation',
  preview_token: 'a'.repeat(64),
  asset_ids: [asset],
};
const ack: CleanupReceipt = {
  operation_id: body.operation_id,
  project_id: project,
  action: 'cleanup',
  receipts: [{ id: receipt, asset_id: asset }],
  policy: 'Recoverable',
};
const preview = (): CleanupPreview => ({
  project_id: project,
  preview_token: body.preview_token,
  candidates: [{ id: asset, hash: 'b'.repeat(64), size_bytes: 1000, duration: 2 }],
  protected_count: 3,
  trash: [],
  policy: 'Recoverable',
});
function memory(map = new Map<string, string>()): RecoveryStorage {
  return {
    get length() {
      return map.size;
    },
    key: (index) => [...map.keys()][index] ?? null,
    getItem: (key) => map.get(key) ?? null,
    setItem: (key, value) => {
      map.set(key, value);
    },
    removeItem: (key) => {
      map.delete(key);
    },
  };
}
let renderer: ReactTestRenderer;
afterEach(() => act(() => renderer?.unmount()));

function text(node: unknown): string {
  if (typeof node === 'string') return node;
  if (Array.isArray(node)) return node.map(text).join('');
  if (node && typeof node === 'object' && 'props' in node)
    return text((node.props as { children?: unknown }).children);
  return '';
}
const button = (label: string) =>
  renderer.root.findAllByType('button').find((item) => text(item.props.children) === label)!;

test('uncertain cleanup survives restart and replays original body before any new intent', async () => {
  const storage = memory(),
    calls: unknown[] = [];
  let lost = true;
  const client = new CleanupClient('http://server', (async (_url, init) => {
    calls.push(JSON.parse(String(init?.body)));
    if (lost) {
      lost = false;
      throw new Error('response lost after commit');
    }
    return Response.json(ack);
  }) as typeof fetch);
  const first = new CleanupSession(storage, client, project);
  await expect(first.submit('cleanup', () => body, null)).rejects.toThrow('response lost');
  const restart = new CleanupSession(storage, client, project);
  expect(calls).toHaveLength(1);
  expect(restart.pending()?.body).toEqual(body);
  await restart.submit(
    'cleanup',
    () => {
      throw new Error('must not create a second intent');
    },
    body.operation_id,
  );
  expect(calls).toEqual([body, body]);
  expect(restart.pending()).toBeNull();
});

test('malformed acknowledgements and local storage failures never clear intent or issue unsafe retries', async () => {
  const storage = memory();
  const client = new CleanupClient('http://server', (async () =>
    Response.json({ ...ack, receipts: [{ id: receipt, asset_id: 'foreign' }] })) as typeof fetch);
  const session = new CleanupSession(storage, client, project);
  await expect(session.submit('cleanup', () => body, null)).rejects.toThrow('acknowledgement');
  expect(session.pending()?.body).toEqual(body);
  expect(() => session.discardRejected()).toThrow('uncertain');
  let calls = 0;
  const transport: CleanupTransport = {
    baseUrl: 'http://server',
    preview: async () => preview(),
    apply: async () => {
      calls++;
      return ack;
    },
  };
  const failed = new CleanupSession(
    {
      ...memory(),
      setItem: () => {
        throw new Error('storage full');
      },
    },
    transport,
    project,
  );
  await expect(failed.submit('cleanup', () => body, null)).rejects.toThrow('storage full');
  expect(calls).toBe(0);
});

test('only definitive rejection can be discarded, and other resource journals remain intact', async () => {
  const storage = memory();
  const client = new CleanupClient('http://server', (async () =>
    Response.json(
      { detail: { code: 'storage_preview_changed', message: 'Refresh preview' } },
      { status: 409 },
    )) as typeof fetch);
  const session = new CleanupSession(storage, client, project);
  await expect(session.submit('cleanup', () => body, null)).rejects.toThrow('Refresh');
  expect(session.pending()?.rejected).toBe(true);
  const other = new CleanupSession(
    storage,
    { ...client, baseUrl: 'http://other-server' } as CleanupTransport,
    project,
  );
  expect(other.pending()).toBeNull();
  session.discardRejected();
  expect(session.pending()).toBeNull();
});

test('failure to remove an acknowledged journal keeps its exact replay identity', async () => {
  const storage = memory();
  const client = new CleanupClient('http://server', (async () =>
    Response.json(ack)) as typeof fetch);
  const failing = new CleanupSession(
    {
      ...storage,
      removeItem: () => {
        throw new Error('cannot clear');
      },
    },
    client,
    project,
  );
  await expect(failing.submit('cleanup', () => body, null)).rejects.toThrow('cannot clear');
  const restart = new CleanupSession(storage, client, project);
  expect(restart.pending()?.body.operation_id).toBe(body.operation_id);
  await restart.submit('cleanup', () => body, body.operation_id);
  expect(restart.pending()).toBeNull();
});

test('cleanup requires reviewed selection and explicit confirmation and exposes recoverable restore', async () => {
  let trashed = false;
  const calls: Array<{ action: string; body: unknown }> = [];
  const client: CleanupTransport = {
    baseUrl: 'http://server',
    preview: async () => ({
      ...preview(),
      candidates: trashed ? [] : preview().candidates,
      trash: trashed ? [{ id: receipt, asset_id: asset, size_bytes: 1000, created_at: 'now' }] : [],
    }),
    apply: async (_project, action, request) => {
      calls.push({ action, body: request });
      trashed = action === 'cleanup';
      return { ...ack, action, operation_id: request.operation_id };
    },
  };
  const storage = memory();
  await act(async () => {
    renderer = create(
      <StorageCleanupPanel client={client} projectId={project} recoveryStorage={storage} />,
    );
  });
  expect(button('Move selected audio to trash').props.disabled).toBe(true);
  act(() => renderer.root.findAllByType('input')[0].props.onChange({ target: { checked: true } }));
  expect(button('Move selected audio to trash').props.disabled).toBe(true);
  act(() => renderer.root.findAllByType('input')[1].props.onChange({ target: { checked: true } }));
  await act(async () => {
    await button('Move selected audio to trash').props.onClick();
  });
  expect(calls[0].action).toBe('cleanup');
  expect(
    renderer.root
      .findAllByType('p')
      .some((item) => text(item.props.children).includes('does not free disk space')),
  ).toBe(true);
  const restoreCheckbox = renderer.root.findAllByType('input').at(-1)!;
  act(() => restoreCheckbox.props.onChange({ target: { checked: true } }));
  await act(async () => {
    await button('Restore selected audio').props.onClick();
  });
  expect(calls.map((item) => item.action)).toEqual(['cleanup', 'restore']);
});

test('component remount offers explicit exact replay after a lost committed response', async () => {
  const storage = memory();
  const calls: unknown[] = [];
  let lost = true;
  const client = new CleanupClient('http://server', (async (_url, init) => {
    if (!init) return Response.json(preview());
    const request = JSON.parse(String(init.body));
    calls.push(request);
    if (lost) {
      lost = false;
      throw new Error('lost acknowledgement');
    }
    return Response.json({ ...ack, operation_id: request.operation_id });
  }) as typeof fetch);
  const session = new CleanupSession(storage, client, project);
  await expect(session.submit('cleanup', () => body, null)).rejects.toThrow('lost');
  await act(async () => {
    renderer = create(
      <StorageCleanupPanel client={client} projectId={project} recoveryStorage={storage} />,
    );
  });
  expect(calls).toHaveLength(1);
  expect(button('Move selected audio to trash').props.disabled).toBe(true);
  await act(async () => {
    await button('Retry original cleanup').props.onClick();
  });
  expect(calls).toEqual([body, body]);
});

test('switching resources fences old acknowledgement and does not unlock a new in-flight request', async () => {
  const storage = memory();
  let resolveOld: (value: CleanupReceipt) => void = () => {};
  let resolveNew: (value: CleanupReceipt) => void = () => {};
  const otherProject = '44444444-4444-4444-8444-444444444444';
  const otherAsset = '55555555-5555-4555-8555-555555555555';
  const first: CleanupTransport = {
    baseUrl: 'http://first',
    preview: async () => preview(),
    apply: async () =>
      new Promise((resolve) => {
        resolveOld = resolve;
      }),
  };
  let otherBody: CleanupBody | undefined;
  const second: CleanupTransport = {
    baseUrl: 'http://second',
    preview: async () => ({
      ...preview(),
      project_id: otherProject,
      candidates: [{ ...preview().candidates[0], id: otherAsset }],
    }),
    apply: async (_project, _action, request) => {
      otherBody = request as CleanupBody;
      return new Promise((resolve) => {
        resolveNew = resolve;
      });
    },
  };
  await act(async () => {
    renderer = create(
      <StorageCleanupPanel client={first} projectId={project} recoveryStorage={storage} />,
    );
  });
  const selectAndConfirm = () => {
    act(() =>
      renderer.root.findAllByType('input')[0].props.onChange({ target: { checked: true } }),
    );
    act(() =>
      renderer.root.findAllByType('input')[1].props.onChange({ target: { checked: true } }),
    );
    act(() => button('Move selected audio to trash').props.onClick());
  };
  selectAndConfirm();
  await act(async () => {
    renderer.update(
      <StorageCleanupPanel client={second} projectId={otherProject} recoveryStorage={storage} />,
    );
  });
  selectAndConfirm();
  await act(async () => {
    resolveOld({
      ...ack,
      operation_id: new CleanupSession(storage, first, project).pending()!.body.operation_id,
    });
  });
  expect(button('Move selected audio to trash').props.disabled).toBe(true);
  expect(renderer.root.findAllByType('output')).toHaveLength(0);
  expect(new CleanupSession(storage, second, otherProject).pending()?.body).toEqual(otherBody);
  await act(async () => {
    resolveNew({
      ...ack,
      project_id: otherProject,
      operation_id: otherBody!.operation_id,
      receipts: [{ id: receipt, asset_id: otherAsset }],
    });
  });
  expect(new CleanupSession(storage, second, otherProject).pending()).toBeNull();
});
