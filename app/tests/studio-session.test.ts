import { describe, expect, test } from 'bun:test';
import { studioShortcut } from '../src/features/studio/keyboard';
import { ProjectSession, type RecoveryStorage } from '../src/features/studio/session';
import type { StudioCommand, StudioProject, StudioTransport } from '../src/features/studio/types';

export function project(revision = 0): StudioProject {
  return {
    id: 'project',
    name: 'A real project',
    client_label: null,
    revision,
    created_at: '',
    updated_at: '',
    takes: [],
    assets: [],
    jobs: [],
    revisions: [],
    approvals: [],
    deliverables: [],
    document: {
      schema_version: 1,
      sample_rate: 48000,
      channels: 2,
      language: 'en',
      passages: [
        {
          id: 'p',
          text: 'Saved text',
          language: 'en',
          profile_id: null,
          voice_snapshot: null,
          direction: { instruct: '', seed: null, max_chunk_chars: 500, crossfade_ms: 0 },
          input_revision: 0,
          selection_token: 0,
          selected_take_id: null,
          locked: false,
        },
      ],
      tracks: [{ id: 'voice', kind: 'voice', name: 'Voice', mute: false, solo: false, gain: 1 }],
      clips: [],
      mix: { target_lufs: -16, max_true_peak_dbtp: -1, normalize: false },
    },
  };
}

test('concurrent windows preserve independent journals when one save succeeds', async () => {
  const { session: first, transport, journal } = setup();
  const second = new ProjectSession(transport, 'project', journal);
  await Promise.all([first.load(), second.load()]);
  first.editPassage('p', { text: 'First window pending' });
  second.editPassage('p', { text: 'Second window pending' });
  expect(journal.getItem(first.key)).toContain('First window pending');
  expect(journal.getItem(second.key)).toContain('Second window pending');
  await first.flush();
  expect(journal.getItem(second.key)).toContain('Second window pending');
  first.dispose();
  second.dispose();
  const reopened = new ProjectSession(transport, 'project', journal);
  await reopened.load();
  expect(reopened.recoveryCopy()).toContain('Second window pending');
  reopened.dispose();
});

test('reopening enumerates every orphan and preserves the legacy journal', async () => {
  const { session: first, transport, journal } = setup();
  const second = new ProjectSession(transport, 'project', journal);
  await Promise.all([first.load(), second.load()]);
  first.editPassage('p', { text: 'Orphan one' });
  second.editPassage('p', { text: 'Orphan two' });
  journal.setItem(
    'voicebox-studio:recovery:http%3A%2F%2Ftest:project',
    JSON.stringify({
      version: 1,
      project_id: 'project',
      base_revision: 0,
      drafts: { p: { text: 'Legacy orphan' } },
      queue: [],
    }),
  );
  first.dispose();
  second.dispose();
  const reopened = new ProjectSession(transport, 'project', journal);
  await reopened.load();
  const copy = reopened.recoveryCopy();
  expect(copy).toContain('Orphan one');
  expect(copy).toContain('Orphan two');
  expect(copy).toContain('Legacy orphan');
  reopened.dispose();
});

test('recovering one orphan leaves the other recoverable and offers it after acknowledgement', async () => {
  const { session: first, transport, journal } = setup();
  const second = new ProjectSession(transport, 'project', journal);
  await Promise.all([first.load(), second.load()]);
  first.editPassage('p', { text: 'Keep first orphan' });
  second.editPassage('p', { text: 'Recover second orphan' });
  first.dispose();
  second.dispose();
  const reopened = new ProjectSession(transport, 'project', journal);
  await reopened.load();
  reopened.recover(second.key);
  expect(reopened.getSnapshot().drafts.p.text).toBe('Recover second orphan');
  await reopened.flush();
  expect(journal.getItem(first.key)).toContain('Keep first orphan');
  expect(reopened.getSnapshot().status).toBe('recovery');
  expect(reopened.recoveryCopy()).toContain('Keep first orphan');
  reopened.dispose();
});
function storage(): RecoveryStorage {
  const values = new Map<string, string>();
  return {
    get length() {
      return values.size;
    },
    key: (index: number) => [...values.keys()][index] ?? null,
    getItem: (k) => values.get(k) ?? null,
    setItem: (k, v) => {
      values.set(k, v);
    },
    removeItem: (k) => {
      values.delete(k);
    },
  };
}
function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((yes, no) => {
    resolve = yes;
    reject = no;
  });
  return { promise, resolve, reject };
}
function setup() {
  let current = project();
  const requests: StudioCommand[] = [];
  const transport: StudioTransport = {
    baseUrl: 'http://test',
    project: async () => structuredClone(current),
    command: async (_id, command) => {
      requests.push(command);
      current = structuredClone(current);
      current.revision++;
      Object.assign(current.document.passages[0], command.payload);
      return current;
    },
    checkpoint: async () => current,
    approve: async () => current,
  };
  const journal = storage();
  const session = new ProjectSession(transport, 'project', journal);
  return { session, transport, requests, journal };
}

describe('studio durable editing boundary', () => {
  test('typing journals locally and Saved waits for acknowledgement, preserving edits made during save', async () => {
    const { session, transport } = setup();
    await session.load();
    const response = deferred<StudioProject>();
    transport.command = async () => response.promise;
    session.editPassage('p', { text: 'First edit' });
    expect(session.getSnapshot().status).toBe('dirty');
    expect(session.recoveryCopy()).toContain('First edit');
    const save = session.flush();
    expect(session.getSnapshot().status).toBe('saving');
    session.editPassage('p', { text: 'Newer typing' });
    const acknowledged = project(1);
    acknowledged.document.passages[0].text = 'First edit';
    response.resolve(acknowledged);
    await save;
    expect(session.getSnapshot().drafts.p.text).toBe('Newer typing');
    expect(session.getSnapshot().status).toBe('dirty');
    session.dispose();
  });

  test('a 409 preserves local text and blocks further mutations until explicit resolution', async () => {
    const { session, transport } = setup();
    await session.load();
    let calls = 0;
    transport.command = async () => {
      calls++;
      throw Object.assign(new Error('Draft changed in another window'), {
        status: 409,
        currentRevision: 3,
      });
    };
    session.editPassage('p', { text: 'Keep my words' });
    await session.flush();
    expect(session.getSnapshot().status).toBe('conflict');
    expect(session.getSnapshot().drafts.p.text).toBe('Keep my words');
    await session.command('undo');
    expect(calls).toBe(1);
    expect(session.recoveryCopy()).toContain('Keep my words');
    await session.reloadSaved();
    expect(session.getSnapshot().status).toBe('saved');
    session.dispose();
  });

  test('retry of an uncertain save reuses the operation id and expected revision', async () => {
    const { session, transport } = setup();
    await session.load();
    const sent: StudioCommand[] = [];
    transport.command = async (_id, input) => {
      sent.push(structuredClone(input));
      if (sent.length === 1) throw new Error('Connection lost after commit');
      return project(1);
    };
    session.editPassage('p', { text: 'Recoverable' });
    await session.flush();
    expect(session.getSnapshot().status).toBe('error');
    await session.retry();
    expect(sent.length).toBe(2);
    expect(sent[1]).toEqual(sent[0]);
    expect(session.getSnapshot().status).toBe('saved');
    session.dispose();
  });

  test('restart offers recovery rather than silently discarding unacknowledged text', async () => {
    const { session, transport, journal } = setup();
    await session.load();
    session.editPassage('p', { text: 'Survives reopening' });
    session.dispose();
    const reopened = new ProjectSession(transport, 'project', journal);
    await reopened.load();
    expect(reopened.getSnapshot().status).toBe('recovery');
    reopened.recover();
    expect(reopened.getSnapshot().drafts.p.text).toBe('Survives reopening');
    await reopened.flush();
    expect(reopened.getSnapshot().status).toBe('saved');
    reopened.dispose();
  });

  test('poll begun before a mutation cannot replace its acknowledgement', async () => {
    const { session, transport } = setup();
    await session.load();
    const stale = deferred<StudioProject>();
    transport.project = () => stale.promise;
    const poll = session.refresh();
    await session.command('update_project', { name: 'New name' });
    stale.resolve(project(0));
    await poll;
    expect(session.getSnapshot().project?.revision).toBe(1);
    session.dispose();
  });

  test('local storage failure is reported without pretending recovery is durable', async () => {
    const { transport } = setup();
    const session = new ProjectSession(transport, 'project', {
      ...storage(),
      setItem() {
        throw new Error('Quota exceeded');
      },
    });
    await session.load();
    session.editPassage('p', { text: 'Still visible' });
    expect(session.getSnapshot().recoverySaved).toBe(false);
    expect(session.getSnapshot().drafts.p.text).toBe('Still visible');
    session.dispose();
  });
});

describe('studio keyboard ownership', () => {
  test('text editors, contentEditable descendants, dialogs and menu items own transport/history keys', () => {
    for (const target of [
      { tagName: 'TEXTAREA' },
      { tagName: 'INPUT' },
      { isContentEditable: true },
      { closest: () => ({ role: 'dialog' }) },
    ]) {
      expect(studioShortcut({ key: ' ', target }, true)).toBeNull();
      expect(studioShortcut({ key: 'z', ctrlKey: true, target }, true)).toBeNull();
    }
  });
  test('timeline shortcuts only run inside timeline focus', () => {
    expect(studioShortcut({ key: 's' }, false)).toBeNull();
    expect(studioShortcut({ key: 's' }, true)).toBe('split');
    expect(studioShortcut({ key: 'd', metaKey: true }, true)).toBe('duplicate');
    expect(studioShortcut({ key: 'z', metaKey: true, shiftKey: true }, false)).toBe('redo');
    expect(studioShortcut({ key: ' ' }, false)).toBe('transport');
  });
});

describe('studio concurrent draft safety', () => {
  test('polling a newer draft while typing cannot silently rebase local changes', async () => {
    const { session, transport, requests } = setup();
    await session.load();
    session.editPassage('p', { text: 'My offline words' });
    transport.project = async () => project(5);
    await session.refresh();
    expect(session.getSnapshot().status).toBe('conflict');
    expect(session.getSnapshot().drafts.p.text).toBe('My offline words');
    await session.flush();
    expect(requests).toHaveLength(0);
    session.dispose();
  });
  test('a rejected validation can be corrected without replaying the invalid payload forever', async () => {
    const { session, transport } = setup();
    await session.load();
    const sent: StudioCommand[] = [];
    transport.command = async (_id, command) => {
      sent.push(command);
      if (command.payload.language === '')
        throw Object.assign(new Error('Language is required'), { status: 422 });
      return project(1);
    };
    session.editPassage('p', { language: '' });
    await session.flush();
    expect(session.getSnapshot().status).toBe('error');
    session.editPassage('p', { language: 'en' });
    await session.retry();
    expect(session.getSnapshot().status).toBe('saved');
    expect(sent).toHaveLength(2);
    expect(sent[1].payload.language).toBe('en');
    expect(sent[1].operation_id).not.toBe(sent[0].operation_id);
    session.dispose();
  });
  test('recovered uncertain operation from initial revision zero can replay against its committed result', async () => {
    const { session, transport, journal } = setup();
    await session.load();
    let savedOperation = '';
    transport.command = async (_id, command) => {
      savedOperation = command.operation_id;
      throw new Error('Response lost');
    };
    session.editPassage('p', { text: 'Committed but not acknowledged' });
    await session.flush();
    session.dispose();
    transport.project = async () => project(1);
    transport.command = async (_id, command) => {
      expect(command.operation_id).toBe(savedOperation);
      expect(command.expected_revision).toBe(0);
      return project(1);
    };
    const reopened = new ProjectSession(transport, 'project', journal);
    await reopened.load();
    reopened.recover();
    expect(reopened.getSnapshot().status).toBe('dirty');
    await reopened.flush();
    expect(reopened.getSnapshot().status).toBe('saved');
    reopened.dispose();
  });
});

test('Space activates focused buttons and links without starting studio transport', () => {
  expect(studioShortcut({ key: ' ', target: { tagName: 'BUTTON' } }, true)).toBeNull();
  expect(studioShortcut({ key: ' ', target: { tagName: 'A' } }, false)).toBeNull();
  expect(studioShortcut({ key: ' ', target: { tagName: 'SUMMARY' } }, false)).toBeNull();
});

test('an unreadable recovery journal is exportable and cannot be silently overwritten', async () => {
  const { session, journal } = setup();
  journal.setItem(session.key, '{partial journal');
  await session.load();
  expect(session.getSnapshot().status).toBe('recovery');
  expect(session.recoveryCopy()).toBe('{partial journal');
  session.editPassage('p', { text: 'Must not replace the old journal' });
  expect(journal.getItem(session.key)).toBe('{partial journal');
  await session.reloadSaved();
  expect(journal.getItem(session.key)).toBeNull();
  session.dispose();
});
test('command palette remains available from a text editor', () => {
  expect(studioShortcut({ key: 'k', metaKey: true, target: { tagName: 'TEXTAREA' } }, false)).toBe(
    'palette',
  );
});

test('a recovery conflict remains a conflict across another restart', async () => {
  const { session, transport, journal } = setup();
  await session.load();
  session.editPassage('p', { text: 'Written against draft zero' });
  session.dispose();
  transport.project = async () => project(3);
  const second = new ProjectSession(transport, 'project', journal);
  await second.load();
  second.recover();
  expect(second.getSnapshot().status).toBe('conflict');
  second.dispose();
  const third = new ProjectSession(transport, 'project', journal);
  await third.load();
  third.recover();
  expect(third.getSnapshot().status).toBe('conflict');
  third.dispose();
});

test('replaying an old acknowledgement never rebases remaining recovery edits onto a newer head', async () => {
  const { session, transport, journal } = setup();
  await session.load();
  transport.command = async () => {
    throw new Error('Response lost');
  };
  session.editPassage('p', { text: 'First request' });
  await session.flush();
  session.dispose();
  transport.project = async () => project(4);
  let calls = 0;
  transport.command = async () => {
    calls++;
    return project(1);
  };
  const reopened = new ProjectSession(transport, 'project', journal);
  await reopened.load();
  reopened.recover();
  reopened.editPassage('p', { text: 'Further recovery edits' });
  await reopened.flush();
  expect(calls).toBe(1);
  expect(reopened.getSnapshot().status).toBe('conflict');
  expect(reopened.getSnapshot().drafts.p.text).toBe('Further recovery edits');
  reopened.dispose();
});
