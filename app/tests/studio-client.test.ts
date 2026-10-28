import { describe, expect, test } from 'bun:test';
import { StudioApiError, StudioClient } from '../src/features/studio/client';
import { preflight, splitScript, toSamples } from '../src/features/studio/domain';
import { DurableSubmission } from '../src/features/studio/submissions';
import type { StudioProject } from '../src/features/studio/types';
import { project as baseProject } from './studio-session.test';

describe('studio contract transport', () => {
  test('commands use explicit revision and operation identity at the studio boundary', async () => {
    let request: { url: string; init?: RequestInit } | undefined;
    const client = new StudioClient('http://server/', (async (url, init) => {
      request = { url: String(url), init };
      return Response.json({ revision: 8 });
    }) as typeof fetch);
    await client.command('a/b', {
      operation_id: 'repeatable',
      expected_revision: 7,
      type: 'undo',
      payload: {},
    });
    expect(request?.url).toBe('http://server/studio/v1/projects/a%2Fb/commands');
    expect(JSON.parse(String(request?.init?.body))).toEqual({
      operation_id: 'repeatable',
      expected_revision: 7,
      type: 'undo',
      payload: {},
    });
  });
  test('conflict exposes server detail and revision, without a hidden retry', async () => {
    let calls = 0;
    const client = new StudioClient('http://server', (async () => {
      calls++;
      return Response.json(
        { detail: { message: 'Another draft committed', current_revision: 9 } },
        { status: 409 },
      );
    }) as typeof fetch);
    try {
      await client.project('p');
      throw new Error('Expected conflict');
    } catch (error) {
      expect(error).toBeInstanceOf(StudioApiError);
      expect((error as StudioApiError).currentRevision).toBe(9);
      expect((error as Error).message).toBe('Another draft committed');
    }
    expect(calls).toBe(1);
  });
  test('generation distinguishes alternative/adopt intent and bounded jobs; exports pin a revision', async () => {
    const bodies: unknown[] = [];
    const client = new StudioClient('http://server', (async (_url, init) => {
      bodies.push(JSON.parse(String(init?.body)));
      return Response.json({ jobs: [], job: {} });
    }) as typeof fetch);
    await client.generate('p', {
      idempotency_key: 'g',
      passage_ids: ['line'],
      count: 3,
      adopt: false,
    });
    await client.render('p', {
      idempotency_key: 'r',
      revision_id: 'immutable',
      kind: 'mix',
      format: 'wav',
    });
    expect(bodies).toEqual([
      { idempotency_key: 'g', passage_ids: ['line'], count: 3, adopt: false },
      { idempotency_key: 'r', revision_id: 'immutable', kind: 'mix', format: 'wav' },
    ]);
  });
});

test('paste preview preserves paragraphs and uses project sample rounding', () => {
  expect(splitScript(' First\nline\r\n\r\n Second \n \nThird ')).toEqual([
    'First\nline',
    'Second',
    'Third',
  ]);
  expect(toSamples(0.00003125)).toBe(2);
});
test('delivery preflight checks the frozen checkpoint rather than the current draft text', () => {
  const doc = {
    schema_version: 1,
    sample_rate: 48000,
    channels: 2,
    language: 'en',
    passages: [{ id: 'p', text: 'Approved words', selected_take_id: 't' }],
    clips: [
      {
        id: 'c',
        passage_id: 'p',
        take_id: 't',
        asset_id: 'a',
        start_sample: 0,
        trim_start_sample: 0,
        trim_end_sample: 0,
        track_id: 'v',
        gain: 1,
        fade_in_samples: 0,
        fade_out_samples: 0,
      },
    ],
    tracks: [{ id: 'v', gain: 1, mute: false, solo: false }],
    mix: { normalize: false },
  } as unknown as StudioProject['document'];
  const project = {
    document: { ...doc, passages: [{ ...doc.passages[0], text: 'New draft' }] },
    assets: [{ id: 'a', frames: 48000, sample_rate: 48000 }],
    takes: [{ id: 't', text: 'Approved words', passage_id: 'p', asset_id: 'a' }],
  } as unknown as StudioProject;
  expect(preflight(doc, project).errors).toEqual([]);
  expect(preflight(project.document, project).errors.join(' ')).toContain('Script changed');
  expect(
    preflight({ ...doc, clips: [{ ...doc.clips[0], asset_id: 'missing' }] }, project).errors.join(
      ' ',
    ),
  ).toContain('missing source audio');
});

test('engine capability lookup respects the frozen model variant rather than the first matching engine', async () => {
  const { engineForPassage } = await import('../src/features/studio/domain');
  const passage = {
    id: 'p',
    voice_snapshot: { engine: 'qwen', model_size: '1.7B' },
  } as unknown as import('../src/features/studio/types').StudioPassage;
  const small = {
    id: 'qwen',
    model_size: '0.6B',
    ready: true,
  } as import('../src/features/studio/types').StudioEngine;
  const large = {
    id: 'qwen',
    model_size: '1.7B',
    ready: false,
  } as import('../src/features/studio/types').StudioEngine;
  expect(engineForPassage(passage, [], [small, large])).toBe(large);
});

function deliveryProject() {
  const p = baseProject();
  p.document.passages[0].selected_take_id = 't';
  p.takes = [
    {
      id: 't',
      passage_id: 'p',
      asset_id: 'a',
      text: 'Saved text',
      request: { passage: { input_revision: 0 } },
      engine: 'e',
      model_size: null,
      created_at: '',
    },
  ];
  p.assets = [
    {
      id: 'a',
      hash: 'h',
      sample_rate: 48000,
      frames: 48000,
      channels: 1,
      duration: 1,
      size_bytes: 10,
    },
  ];
  p.document.clips = [
    {
      id: 'c',
      passage_id: 'p',
      take_id: 't',
      asset_id: 'a',
      track_id: 'voice',
      start_sample: 0,
      trim_start_sample: 0,
      trim_end_sample: 0,
      gain: 1,
      fade_in_samples: 0,
      fade_out_samples: 0,
    },
  ];
  return p;
}
test('delivery blocks a narration passage omitted from an otherwise valid mix', () => {
  const p = deliveryProject();
  p.document.passages.push({
    ...p.document.passages[0],
    id: 'omitted',
    text: 'Do not silently skip me',
    selected_take_id: null,
  });
  expect(preflight(p.document, p).errors.join(' ')).toContain('selected');
});
test('delivery requires each selected narration take to have an audible matching clip', () => {
  for (const change of [
    'muted',
    'zero-track',
    'zero-clip',
    'solo-other',
    'missing-track',
    'wrong-take',
    'wrong-asset',
  ] as const) {
    const p = deliveryProject();
    if (change === 'muted') p.document.tracks[0].mute = true;
    if (change === 'zero-track') p.document.tracks[0].gain = 0;
    if (change === 'zero-clip') p.document.clips[0].gain = 0;
    if (change === 'solo-other')
      p.document.tracks.push({ ...p.document.tracks[0], id: 'other', solo: true });
    if (change === 'missing-track') p.document.clips[0].track_id = 'missing';
    if (change === 'wrong-take') p.document.clips[0].take_id = 'unrelated';
    if (change === 'wrong-asset') p.document.clips[0].asset_id = 'wrong';
    expect(preflight(p.document, p).errors.length, change).toBeGreaterThan(0);
  }
});
test('explicit earlier-recording consent permits stale inputs but never missing relationships or audio', () => {
  const p = deliveryProject();
  p.document.passages[0].text = 'Revised script';
  p.document.passages[0].input_revision = 2;
  expect(preflight(p.document, p).errors.join(' ')).toContain('Script changed');
  expect(preflight(p.document, p, true).errors).toEqual([]);
  p.document.clips[0].asset_id = 'missing';
  expect(preflight(p.document, p, true).errors.length).toBeGreaterThan(0);
});

test('keeping earlier timeline audio remains deliverable when its selected take differs', () => {
  const p = deliveryProject();
  p.takes.push({ ...p.takes[0], id: 'new-take' });
  p.document.passages[0].selected_take_id = 'new-take';
  expect(preflight(p.document, p).errors).toEqual([]);
  expect(preflight(p.document, p).warnings.join(' ')).toContain('differs');
});

test('a muted solo track does not remove narration coverage', () => {
  const p = deliveryProject();
  p.document.tracks.push({ ...p.document.tracks[0], id: 'muted-solo', mute: true, solo: true });
  expect(preflight(p.document, p).errors).toEqual([]);
});

test('a frozen variant absent from the registry cannot borrow generic engine capabilities', async () => {
  const { engineForPassage } = await import('../src/features/studio/domain');
  const p = baseProject().document.passages[0];
  p.voice_snapshot = { engine: 'e', model_size: 'retired' };
  expect(
    engineForPassage(
      p,
      [],
      [{ id: 'e', ready: true } as import('../src/features/studio/types').StudioEngine],
    ),
  ).toBeUndefined();
});

function submissionStorage() {
  const values = new Map<string, string>();
  return {
    values,
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
}
test('durable submissions never retry implicitly and clear only their own acknowledged identity', async () => {
  const storage = submissionStorage();
  const first = new DurableSubmission(storage, 'http://server', 'p', 'generation');
  const second = new DurableSubmission(storage, 'http://server', 'p', 'generation');
  let finish!: () => void;
  const response = new Promise<void>((resolve) => {
    finish = resolve;
  });
  const original = {
    idempotency_key: 'original',
    passage_ids: ['one'],
    count: 3 as const,
    adopt: false,
  };
  const sent: unknown[] = [];
  const pending = first.submit(
    () => original,
    async (body) => {
      sent.push(body);
      await response;
    },
  );
  const originalRecord = first.pending()[0];
  // A second independent window may already have issued a different durable intent.
  const otherKey = originalRecord.key.replace('original', 'independent');
  storage.setItem(
    otherKey,
    JSON.stringify({
      version: 1,
      kind: 'generation',
      body: { ...original, idempotency_key: 'independent' },
    }),
  );
  expect(second.pending()).toHaveLength(2);
  expect(sent).toHaveLength(1);
  finish();
  await pending;
  expect(storage.getItem(otherKey)).not.toBeNull();
  expect(storage.getItem(originalRecord.key)).toBeNull();
});
test('durable render refuses silent storage drops before network publication', async () => {
  const storage = submissionStorage();
  storage.setItem = () => {};
  const render = new DurableSubmission(storage, 'http://server', 'p', 'render');
  let calls = 0;
  await expect(
    render.submit(
      () => ({ idempotency_key: 'render', revision_id: 'frozen', kind: 'mix', format: 'wav' }),
      async () => {
        calls++;
      },
    ),
  ).rejects.toThrow();
  expect(calls).toBe(0);
});
test('only a definitive validation rejection may be explicitly dismissed before a new identity', async () => {
  const storage = submissionStorage();
  const submission = new DurableSubmission(storage, 'http://server', 'p', 'render');
  const input = {
    idempotency_key: 'r',
    revision_id: 'checkpoint',
    kind: 'mix' as const,
    format: 'wav' as const,
  };
  await expect(
    submission.submit(
      () => input,
      async () => {
        throw Object.assign(new Error('Invalid checkpoint'), { status: 422 });
      },
    ),
  ).rejects.toThrow();
  const reopened = new DurableSubmission(storage, 'http://server', 'p', 'render');
  expect(reopened.pending()[0].rejected).toBe(true);
  reopened.discardRejected();
  expect(reopened.pending()).toHaveLength(0);
  await expect(
    submission.submit(
      () => input,
      async () => {
        throw new Error('Response lost');
      },
    ),
  ).rejects.toThrow();
  expect(() => submission.discardRejected()).toThrow();
  expect(submission.pending()[0].body).toEqual(input);
});

test('a concurrent pending request arriving after the displayed state cannot trigger an unrequested replay', async () => {
  const storage = submissionStorage();
  const submission = new DurableSubmission(storage, 'http://server', 'p', 'render');
  const original = {
    idempotency_key: 'r',
    revision_id: 'checkpoint',
    kind: 'mix' as const,
    format: 'wav' as const,
  };
  await expect(
    submission.submit(
      () => original,
      async () => {
        throw new Error('Response lost');
      },
    ),
  ).rejects.toThrow();
  let calls = 0;
  await expect(
    submission.submit(
      () => ({ ...original, idempotency_key: 'new' }),
      async () => {
        calls++;
      },
      undefined,
      null,
    ),
  ).rejects.toThrow('changed');
  expect(calls).toBe(0);
  expect(submission.pending()[0].body).toEqual(original);
});

test('retrying a rejected request revokes dismissal before a later response can become uncertain', async () => {
  const storage = submissionStorage();
  const submission = new DurableSubmission(storage, 'http://server', 'p', 'render');
  const original = {
    idempotency_key: 'r',
    revision_id: 'checkpoint',
    kind: 'mix' as const,
    format: 'wav' as const,
  };
  await expect(
    submission.submit(
      () => original,
      async () => {
        throw Object.assign(new Error('Validation rejected'), { status: 422 });
      },
    ),
  ).rejects.toThrow();
  expect(submission.pending()[0].rejected).toBe(true);
  await expect(
    submission.submit(
      () => original,
      async () => {
        throw new Error('Response lost after acceptance');
      },
    ),
  ).rejects.toThrow();
  expect(submission.pending()[0].rejected).toBe(false);
  expect(() => submission.discardRejected()).toThrow();
  expect(submission.pending()[0].body).toEqual(original);
});
