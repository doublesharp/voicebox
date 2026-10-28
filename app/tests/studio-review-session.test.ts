import { describe, expect, test } from 'bun:test';
import { StudioApiError } from '../src/features/studio/client';
import type {
  CheckpointReview,
  ReviewRequest,
  ReviewTransport,
} from '../src/features/studio/review-client';
import { ReviewSession, ReviewSessions } from '../src/features/studio/review-session';

function fixture() {
  const data = new Map<string, string>();
  let failStorage = false;
  const storage = {
    get length() {
      return data.size;
    },
    key: (i: number) => [...data.keys()][i] ?? null,
    getItem: (key: string) => data.get(key) ?? null,
    setItem: (key: string, value: string) => {
      if (failStorage) throw new Error('Quota');
      data.set(key, value);
    },
    removeItem: (key: string) => {
      if (failStorage) throw new Error('Quota');
      data.delete(key);
    },
  };
  let current: CheckpointReview = {
    revision_id: 'r',
    version: 0,
    note: '',
    checklist: [],
    updated_at: null,
  };
  const calls: { kind: string; body: unknown }[] = [];
  const committed = new Map<string, { body: string; review: CheckpointReview }>();
  const approvals: CheckpointReview[] = [];
  let lose = false;
  let release: (() => void) | null = null;
  let gate: Promise<void> | null = null;
  const client: ReviewTransport = {
    baseUrl: 'http://review',
    get: async () => ({ review: structuredClone(current) }),
    save: async (_p, _r, body) => {
      calls.push({ kind: 'save', body: structuredClone(body) });
      if (gate) await gate;
      const prior = committed.get(body.operation_id);
      if (prior) {
        if (prior.body !== JSON.stringify(body)) throw new StudioApiError('Identity mismatch', 409);
        return { review: structuredClone(prior.review) };
      }
      if (body.expected_version !== current.version) throw new StudioApiError('Conflict', 409);
      current = {
        revision_id: 'r',
        version: current.version + 1,
        note: body.note,
        checklist: structuredClone(body.checklist),
        updated_at: 'now',
      };
      committed.set(body.operation_id, {
        body: JSON.stringify(body),
        review: structuredClone(current),
      });
      if (lose) {
        lose = false;
        throw new Error('Response lost after commit');
      }
      return { review: structuredClone(current) };
    },
    approve: async (_p, _r, body) => {
      calls.push({ kind: 'approval', body: structuredClone(body) });
      const prior = committed.get(body.operation_id);
      if (prior) return { review: prior.review };
      if (!current.version || current.version !== body.expected_version)
        throw new StudioApiError('Conflict', 409);
      const frozen = structuredClone(current);
      approvals.push(frozen);
      committed.set(body.operation_id, { body: JSON.stringify(body), review: frozen });
      if (lose) {
        lose = false;
        throw new Error('Approval response lost');
      }
      return { review: frozen };
    },
  };
  return {
    data,
    storage,
    client,
    calls,
    approvals,
    failStorage: (v: boolean) => {
      failStorage = v;
    },
    lose: () => {
      lose = true;
    },
    block: () => {
      gate = new Promise<void>((r) => {
        release = r;
      });
    },
    unblock: () => {
      gate = null;
      release?.();
    },
    advance: () => {
      current = { ...current, version: current.version + 1, note: 'Other window' };
    },
    session: () => new ReviewSession(client, 'p', 'r', storage),
  };
}
const turn = () => new Promise<void>((resolve) => setTimeout(resolve, 0));
describe('checkpoint review session owns recovery and acknowledgement', () => {
  test('every keystroke persists before debounce, restart recovery keeps original version across polls', async () => {
    const f = fixture();
    const a = f.session();
    await a.load();
    a.edit({ note: 'Unacknowledged' });
    expect(JSON.parse(f.data.get(a.key)!).draft.note).toBe('Unacknowledged');
    a.dispose();
    f.advance();
    const b = f.session();
    await b.load();
    expect(b.getSnapshot().status).toBe('recovery');
    b.recover(b.getSnapshot().recoveries[0].key);
    expect(b.getSnapshot().status).toBe('conflict');
    expect(b.value().note).toBe('Unacknowledged');
    expect(JSON.parse(f.data.get(b.key)!).base_version).toBe(0);
    expect(await b.approve()).toBe(false);
    expect(f.calls).toHaveLength(0);
    b.dispose();
  });
  test('uncertain successful save restarts and replays exact identity and version before newer typing', async () => {
    const f = fixture();
    const a = f.session();
    await a.load();
    f.lose();
    a.edit({ note: 'First' });
    await a.flush();
    expect(a.getSnapshot().status).toBe('error');
    a.edit({ note: 'Newer' });
    a.dispose();
    const b = f.session();
    await b.load();
    b.recover(b.getSnapshot().recoveries[0].key);
    await b.flush();
    expect(f.calls[1]).toEqual(f.calls[0]);
    expect((f.calls[2].body as ReviewRequest).expected_version).toBe(1);
    expect(b.value().note).toBe('Newer');
    expect(b.getSnapshot().status).toBe('saved');
    b.dispose();
  });
  test('new typing during acknowledgement is saved with its own body and retained until acknowledged', async () => {
    const f = fixture();
    const a = f.session();
    await a.load();
    f.block();
    a.edit({ note: 'First' });
    const saving = a.flush();
    await turn();
    a.edit({ note: 'Newer' });
    expect(JSON.parse(f.data.get(a.key)!).draft.note).toBe('Newer');
    f.unblock();
    await saving;
    expect(f.calls.map((c) => (c.body as ReviewRequest).note)).toEqual(['First', 'Newer']);
    expect(a.getSnapshot().status).toBe('saved');
    a.dispose();
  });
  test('polling detects another window without rebasing original edit version', async () => {
    const f = fixture();
    const a = f.session();
    const b = f.session();
    await a.load();
    await b.load();
    a.edit({ note: 'A' });
    b.edit({ note: 'B' });
    await b.flush();
    await a.refresh();
    expect(a.getSnapshot().status).toBe('conflict');
    expect(a.value().note).toBe('A');
    expect(JSON.parse(f.data.get(a.key)!).base_version).toBe(0);
    expect(await a.flush()).toBe(false);
    expect(f.calls).toHaveLength(1);
    a.dispose();
    b.dispose();
  });
  test('separate windows retain their own journals; recovery consumes only an exact snapshot', async () => {
    const f = fixture();
    const a = f.session();
    const b = f.session();
    await a.load();
    await b.load();
    a.edit({ note: 'A' });
    b.edit({ note: 'B' });
    const rawA = f.data.get(a.key);
    const rawB = f.data.get(b.key);
    a.dispose();
    b.dispose();
    const c = f.session();
    await c.load();
    expect(c.getSnapshot().recoveries).toHaveLength(2);
    c.recover(a.key);
    expect(c.value().note).toBe('A');
    expect(f.data.get(a.key)).toBe(rawA);
    expect(f.data.get(b.key)).toBe(rawB);
    c.dispose();
  });
  test('selecting a second orphan cannot overwrite unacknowledged typing from the first', async () => {
    const f = fixture();
    const a = f.session();
    const b = f.session();
    await a.load();
    await b.load();
    a.edit({ note: 'A' });
    b.edit({ note: 'B' });
    a.dispose();
    b.dispose();
    const c = f.session();
    await c.load();
    c.recover(a.key);
    c.edit({ note: 'New A typing' });
    c.recover(b.key);
    expect(c.value().note).toBe('New A typing');
    expect(c.recoveryCopy()).toContain('New A typing');
    expect(c.getSnapshot().recoveries.map((r) => r.key)).toContain(b.key);
    c.dispose();
  });
  test('discarding an active conflict never consumes another windows offered journal', async () => {
    const f = fixture();
    const a = f.session();
    await a.load();
    a.edit({ note: 'Other window' });
    a.dispose();
    const b = f.session();
    await b.load();
    await b.reloadSaved();
    b.dispose();
    const c = f.session();
    await c.load();
    expect(c.getSnapshot().recoveries.map((r) => r.key)).toContain(a.key);
    c.dispose();
  });
  test('storage failure blocks requests and route guard while keeping an exportable live copy', async () => {
    const f = fixture();
    const group = new ReviewSessions(f.client, 'p', f.storage);
    const a = group.get('r');
    await turn();
    f.failStorage(true);
    a.edit({ note: 'Keep open' });
    expect(group.unsafeToLeave()).toBe(true);
    expect(await a.flush()).toBe(false);
    expect(await a.approve()).toBe(false);
    expect(f.calls).toHaveLength(0);
    expect(a.recoveryCopy()).toContain('Keep open');
    f.failStorage(false);
    expect(await a.retry()).toBe(true);
    expect(group.unsafeToLeave()).toBe(false);
    group.dispose();
  });
  test('approval waits for save and freezes unchecked state; subsequent edits do not mutate approval', async () => {
    const f = fixture();
    const a = f.session();
    await a.load();
    a.edit({ note: 'Checked note' });
    expect(await a.approve()).toBe(true);
    expect(f.calls.map((c) => c.kind)).toEqual(['save', 'approval']);
    expect(f.approvals[0].checklist.every((i) => !i.checked)).toBe(true);
    a.edit({ note: 'Later' });
    await a.flush();
    expect(f.approvals[0].note).toBe('Checked note');
    a.dispose();
  });
  test('uncertain approval replay retains exact operation and freezes the original snapshot', async () => {
    const f = fixture();
    const a = f.session();
    await a.load();
    a.edit({ note: 'Original' });
    await a.flush();
    f.lose();
    expect(await a.approve()).toBe(false);
    a.edit({ note: 'Later' });
    a.dispose();
    const b = f.session();
    await b.load();
    b.recover(b.getSnapshot().recoveries[0].key);
    await b.flush();
    expect(f.calls[2]).toEqual(f.calls[1]);
    expect(f.approvals).toHaveLength(1);
    expect(f.approvals[0].note).toBe('Original');
    expect(b.value().note).toBe('Later');
    b.dispose();
  });
  test('an older overlapping initial read cannot replace newer loaded review or clear typing status', async () => {
    const f = fixture();
    const releases: Array<(value: { review: CheckpointReview }) => void> = [];
    f.client.get = () => new Promise((resolve) => releases.push(resolve));
    const a = f.session();
    const older = a.load();
    const newer = a.load();
    releases[1]({
      review: {
        revision_id: 'r',
        version: 2,
        note: 'New server',
        checklist: [],
        updated_at: 'now',
      },
    });
    await newer;
    a.edit({ note: 'New typing' });
    releases[0]({
      review: {
        revision_id: 'r',
        version: 1,
        note: 'Old server',
        checklist: [],
        updated_at: 'now',
      },
    });
    await older;
    expect(a.getSnapshot().review?.version).toBe(2);
    expect(a.getSnapshot().status).toBe('dirty');
    expect(a.value().note).toBe('New typing');
    a.dispose();
  });
  test('unreadable journals remain exportable and never become empty drafts', async () => {
    const f = fixture();
    const a = f.session();
    f.data.set(`${a.prefix}broken`, '{broken');
    await a.load();
    expect(a.getSnapshot().status).toBe('recovery');
    expect(a.getSnapshot().recoveries[0].readable).toBe(false);
    expect(a.recoveryCopy()).toContain('{broken');
    expect(await a.approve()).toBe(false);
    a.dispose();
  });
});
