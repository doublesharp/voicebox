import { afterEach, expect, test } from 'bun:test';
import { act, create, type ReactTestRenderer } from 'react-test-renderer';
import { StudioContext, type StudioContextValue } from '../src/features/studio/context';
import { SpeechReviewPanel } from '../src/features/studio/SpeechReviewPanel';
import { type SpeechReview, SpeechReviewClient } from '../src/features/studio/speech-review-client';

let renderer: ReactTestRenderer | undefined;
const originalFetch = globalThis.fetch;
afterEach(() => {
  act(() => renderer?.unmount());
  renderer = undefined;
  globalThis.fetch = originalFetch;
});
const report: SpeechReview = {
  version: 1,
  advisory: true,
  source_key: 'a'.repeat(64),
  source: {
    asset_hash: 'b'.repeat(64),
    take_text: 'Hello Ada',
    take_engine: 'fixture',
    take_model: null,
    request_sha256: 'c'.repeat(64),
    script_text: 'Hello Ada',
    language: 'en',
    duration: 2,
  },
  recognized_text: 'Hello Eva',
  words: [
    { word: 'Hello', start: 0, end: 0.5 },
    { word: 'Eva', start: 0.5, end: 1 },
  ],
  differences: [
    {
      kind: 'substitution',
      expected: 'Ada',
      recognized: 'Eva',
      expected_start: 1,
      expected_end: 2,
      recognized_start: 1,
      recognized_end: 2,
      audition: { start: 0.5, end: 1 },
    },
  ],
  recognizer: { backend: 'fixture', model_size: 'base' },
};
function content(value: unknown): string {
  if (typeof value === 'string' || typeof value === 'number') return String(value);
  if (Array.isArray(value)) return value.map(content).join('');
  return '';
}
const button = (label: string) =>
  renderer!.root.findAllByType('button').find((node) => content(node.props.children) === label)!;
async function fixture(savedReport = false) {
  const commands: unknown[] = [],
    repairs: unknown[] = [],
    auditions: unknown[] = [];
  const passage = { id: 'line', text: 'Hello Ada', language: 'en', selected_take_id: 'take' };
  const context = {
    project: {
      id: 'project',
      revision: 2,
      document: { passages: [passage], speech_reviews: savedReport ? [report] : [] },
      takes: [
        {
          id: 'take',
          passage_id: 'line',
          asset_id: 'asset',
          text: 'Hello Ada',
          engine: 'fixture',
          model_size: null,
        },
      ],
      assets: [{ id: 'asset', hash: 'b'.repeat(64) }],
    },
    client: { baseUrl: 'http://local', assetAudio: () => '/asset.wav' },
    session: {
      flush: async () => {},
      getSnapshot: () => ({ status: 'saved' }),
      command: async (...args: unknown[]) => {
        commands.push(args);
        return true;
      },
    },
    playback: {
      audition: async (...args: unknown[]) => {
        auditions.push(args);
      },
    },
    canMutate: true,
    actionBusy: false,
    runAction: async (action: () => Promise<unknown>) => action(),
  } as unknown as StudioContextValue;
  const tree = () => (
    <StudioContext.Provider value={context}>
      <SpeechReviewPanel passage={passage as never} onRepair={(...args) => repairs.push(args)} />
    </StudioContext.Provider>
  );
  await act(async () => {
    renderer = create(tree());
  });
  return {
    commands,
    repairs,
    auditions,
    context,
    rerender: async () => {
      await act(async () => renderer!.update(tree()));
    },
  };
}

test('explicit speech check persists the advisory proposal without selecting or generating audio', async () => {
  const requests: unknown[] = [];
  globalThis.fetch = (async (_url, init) => {
    requests.push(JSON.parse(String(init?.body)));
    return Response.json(report);
  }) as typeof fetch;
  const value = await fixture();
  expect(requests).toEqual([]);
  await act(async () => {
    await button('Check and save speech review').props.onClick();
  });
  expect(requests).toEqual([{ take_id: 'take', expected_revision: 2 }]);
  expect(value.commands).toEqual([['set_speech_review', { take_id: 'take', report }]]);
  expect(value.repairs).toEqual([]);
});

test('saved recognized differences offer audition and explicit repair preparation', async () => {
  const value = await fixture(true);
  expect(JSON.stringify(renderer!.toJSON())).toContain('not forced alignment');
  await act(async () => {
    await button('Listen to suspected interval').props.onClick();
  });
  expect(value.auditions).toEqual([
    ['/asset.wav', 'Speech review interval', 1, { start: 0.5, end: 1 }],
  ]);
  act(() => button('Prepare replacement phrase').props.onClick());
  expect(value.repairs).toEqual([['take', report.differences[0]]]);
  expect(value.commands).toEqual([]);
  value.context.project.document.passages[0].text = 'The script changed';
  await value.rerender();
  expect(button('Prepare replacement phrase')).toBeUndefined();
});

test('late recognition is discarded after a project revision change', async () => {
  let resolve!: (response: Response) => void;
  globalThis.fetch = (() =>
    new Promise<Response>((done) => {
      resolve = done;
    })) as typeof fetch;
  const value = await fixture();
  let pending: Promise<void>;
  await act(async () => {
    pending = button('Check and save speech review').props.onClick();
  });
  value.context.project.revision++;
  await value.rerender();
  await act(async () => {
    resolve(Response.json(report));
    await pending;
  });
  expect(value.commands).toEqual([]);
});

test('missing model and malformed word metadata never become saved reports', async () => {
  const missing = new SpeechReviewClient('http://local', (async () =>
    Response.json({ detail: 'Download Whisper first' }, { status: 422 })) as typeof fetch);
  await expect(missing.recognize('project', 'take', 2)).rejects.toThrow('Download Whisper first');
  const invalid = new SpeechReviewClient('http://local', (async () =>
    Response.json({ ...report, words: [{ word: 'Ada', start: 2, end: 1 }] })) as typeof fetch);
  await expect(invalid.recognize('project', 'take', 2)).rejects.toThrow(
    'Invalid recognized speech',
  );
});

test('unsaved projects and uncertain review storage do not appear successful', async () => {
  globalThis.fetch = (async () => Response.json(report)) as typeof fetch;
  const value = await fixture();
  value.context.session.getSnapshot = (() => ({ status: 'error' })) as never;
  await act(async () => {
    await button('Check and save speech review').props.onClick();
  });
  expect(JSON.stringify(renderer!.toJSON())).toContain('Save and reconcile');
  expect(value.commands).toEqual([]);
  value.context.session.getSnapshot = (() => ({ status: 'saved' })) as never;
  value.context.session.command = async () => false;
  await act(async () => {
    await button('Check and save speech review').props.onClick();
  });
  expect(JSON.stringify(renderer!.toJSON())).toContain('not acknowledged');
});

test('a pending local edit or absent take prevents applying a recognition response', async () => {
  globalThis.fetch = (async () => Response.json(report)) as typeof fetch;
  const value = await fixture();
  value.context.session.getSnapshot = (() => ({
    status: 'saved',
    project: { revision: 3 },
  })) as never;
  await act(async () => {
    await button('Check and save speech review').props.onClick();
  });
  expect(JSON.stringify(renderer!.toJSON())).toContain('changed during recognition');
  expect(value.commands).toEqual([]);
  value.context.project.takes = [];
  await value.rerender();
  expect(renderer!.toJSON()).toBeNull();
});
