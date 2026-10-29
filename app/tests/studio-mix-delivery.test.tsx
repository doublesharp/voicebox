import { afterEach, expect, test } from 'bun:test';
import { isValidElement, type ReactElement, type ReactNode } from 'react';
import { act, create, type ReactTestRenderer } from 'react-test-renderer';
import { DialogContent } from '../src/components/ui/dialog';
import { StudioContext, type StudioContextValue } from '../src/features/studio/context';
import { Deliver } from '../src/features/studio/Deliver';
import { PlatformProvider } from '../src/platform/PlatformContext';
import type { Platform } from '../src/platform/types';

let renderer: ReactTestRenderer;
afterEach(() => act(() => renderer?.unmount()));

function elements() {
  function visit(
    node: ReactNode,
  ): ReactElement<{ children?: ReactNode; [key: string]: unknown }>[] {
    if (Array.isArray(node)) return node.flatMap(visit);
    if (!isValidElement<{ children?: ReactNode; [key: string]: unknown }>(node)) return [];
    return [node, ...visit(node.props.children)];
  }
  return visit(renderer.root.findByType(DialogContent).props.children);
}
function content(node: ReactNode): string {
  if (typeof node === 'string' || typeof node === 'number') return String(node);
  if (Array.isArray(node)) return node.map(content).join('');
  return '';
}
function findButton(text: string) {
  const node = elements().find(
    (item) =>
      content(item.props.children).includes(text) && typeof item.props.onClick === 'function',
  );
  expect(node).toBeDefined();
  return node!;
}

test('delivery submits selected processing and reports native publication receipts accurately', async () => {
  const doc = {
    passages: [],
    tracks: [{ id: 't', gain: 1, mute: false, solo: false }],
    clips: [
      {
        id: 'c',
        asset_id: 'a',
        track_id: 't',
        start_sample: 0,
        trim_start_sample: 0,
        trim_end_sample: 0,
        gain: 1,
        fade_in_samples: 0,
        fade_out_samples: 0,
      },
    ],
    sample_rate: 48000,
    mix: { normalize: false, target_lufs: -16, max_true_peak_dbtp: -1.5 },
  };
  const project = {
    id: 'p',
    name: 'Mix',
    revisions: [{ id: 'r', name: 'Ready', document: doc }],
    document: doc,
    takes: [],
    assets: [{ id: 'a', frames: 100, sample_rate: 48000 }],
    jobs: [],
    deliverables: [
      {
        id: 'f',
        revision_id: 'r',
        filename: 'render.wav',
        kind: 'mix',
        format: 'wav',
        processing: 'dry',
      },
    ],
  };
  const records = new Map<string, string>();
  const storage = {
    get length() {
      return records.size;
    },
    key: (i: number) => [...records.keys()][i] ?? null,
    getItem: (k: string) => records.get(k) ?? null,
    setItem: (k: string, v: string) => {
      records.set(k, v);
    },
    removeItem: (k: string) => {
      records.delete(k);
    },
  };
  const requests: unknown[] = [];
  const saves: string[] = [];
  const errors: string[] = [];
  let flushed = 0;
  let draftStatus = 'conflict';
  let status: 'saved' | 'cancelled' | 'download_started' = 'cancelled';
  const client = {
    baseUrl: 'http://test',
    render: async (_: string, body: unknown) => {
      requests.push(body);
    },
    deliverableDownload: () => 'http://test/file',
    deliverableAudio: () => 'http://test/audio',
    download: async () => new Blob(['audio']),
    archiveUrl: () => 'http://test/archive',
  };
  const context = {
    project,
    client,
    session: {
      storage,
      flush: async () => {
        flushed++;
      },
      getSnapshot: () => ({ status: draftStatus, recoverySaved: true }),
    },
    revisionId: 'r',
    setRevisionId: () => {},
    actionBusy: false,
    actionError: null,
    runAction: async (action: () => Promise<void>) => {
      try {
        await action();
      } catch (error) {
        errors.push(String(error));
      }
    },
    playback: { audition: async () => {} },
  } as unknown as StudioContextValue;
  const filesystem: {
    saveFromUrl?: (_name: string, url: string) => Promise<{ status: typeof status }>;
    saveFile: () => Promise<void>;
  } = {
    saveFromUrl: async (_name: string, url: string) => {
      saves.push(url);
      return { status };
    },
    saveFile: async () => {},
  };
  const platform = { filesystem } as unknown as Platform;
  await act(async () => {
    renderer = create(
      <PlatformProvider platform={platform}>
        <StudioContext.Provider value={context}>
          <Deliver />
        </StudioContext.Provider>
      </PlatformProvider>,
    );
  });
  const select = elements().find(
    (item) => item.type === 'select' && item.props.value === 'processed',
  )!;
  act(() => select.props.onChange({ target: { value: 'dry' } }));
  await act(async () => {
    await findButton('Render dry mix').props.onClick();
  });
  expect(requests[0]).toMatchObject({ revision_id: 'r', kind: 'mix', processing: 'dry' });
  await act(async () => {
    await findButton('Save a copy').props.onClick();
  });
  expect(saves).toEqual(['http://test/file']);
  expect(elements().some((node) => content(node.props.children) === 'Save cancelled.')).toBe(true);
  status = 'download_started';
  await act(async () => {
    await findButton('Save a copy').props.onClick();
  });
  expect(elements().some((node) => content(node.props.children) === 'Download started.')).toBe(
    true,
  );
  status = 'saved';
  await act(async () => {
    await findButton('Save a copy').props.onClick();
  });
  expect(elements().some((node) => content(node.props.children) === 'File saved.')).toBe(true);
  delete filesystem.saveFromUrl;
  await act(async () => {
    await findButton('Save a copy').props.onClick();
  });
  expect(
    elements().some(
      (node) =>
        content(node.props.children) === 'Save requested; destination confirmation unavailable.',
    ),
  ).toBe(true);
  filesystem.saveFromUrl = async (_name: string, url: string) => {
    saves.push(url);
    return { status };
  };
  const before = saves.length;
  await act(async () => {
    findButton('Export complete project archive').props.onClick();
  });
  expect(flushed).toBe(1);
  expect(saves).toHaveLength(before);
  expect(errors[0]).toContain('Save the current draft');
  draftStatus = 'saved';
  await act(async () => {
    findButton('Export complete project archive').props.onClick();
  });
  expect(saves.at(-1)).toBe(
    'http://test/archive?take_scope=all&include_regeneration_references=true',
  );
});
