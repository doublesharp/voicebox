import { afterEach, expect, test } from 'bun:test';
import { act, create, type ReactTestRenderer } from 'react-test-renderer';
import { CaptionsPanel } from '../src/features/studio/CaptionsPanel';
import { CaptionsClient, type CaptionStatus } from '../src/features/studio/captions-client';
import { StudioContext, type StudioContextValue } from '../src/features/studio/context';
import { PlatformProvider } from '../src/platform/PlatformContext';
import type { Platform } from '../src/platform/types';

let renderer: ReactTestRenderer | undefined;
const originalFetch = globalThis.fetch;
const originalCreate = URL.createObjectURL;
const originalRevoke = URL.revokeObjectURL;
afterEach(() => {
  act(() => renderer?.unmount());
  renderer = undefined;
  globalThis.fetch = originalFetch;
  URL.createObjectURL = originalCreate;
  URL.revokeObjectURL = originalRevoke;
});
function text(value: unknown): string {
  if (typeof value === 'string' || typeof value === 'number') return String(value);
  if (Array.isArray(value)) return value.map(text).join('');
  if (value && typeof value === 'object' && 'props' in value)
    return text((value.props as { children?: unknown }).children);
  return '';
}
const button = (label: string) =>
  renderer!.root.findAllByType('button').find((node) => text(node.props.children) === label)!;
const status = (): CaptionStatus => ({
  source_fingerprint: 'a'.repeat(64),
  current: true,
  reviewed: true,
  exportable: true,
  captions: null,
  scene_timing: [],
});
function fixture(current = status(), playbackMode = 'idle') {
  const commands: Array<{ type: string; payload: unknown }> = [];
  const saves: string[] = [];
  const playback = { mode: playbackMode };
  const context = {
    project: {
      id: 'project',
      name: 'A film',
      revision: 3,
      document: {
        sample_rate: 48000,
        passages: [{ id: 'passage', text: 'A short phrase', selected_take_id: 'take' }],
        tracks: [],
        clips: [],
        captions: {
          source_fingerprint: 'a'.repeat(64),
          reviewed: true,
          cues: [
            {
              id: 'cue',
              clip_id: null,
              start_sample: 48000,
              end_sample: 96000,
              text: 'A short phrase',
            },
          ],
        },
      },
    },
    client: { baseUrl: 'http://server', download: async () => new Blob(['captions']) },
    playback: { subscribe: () => () => {}, getSnapshot: () => playback },
    session: {
      command: async (type: string, payload: unknown) => {
        commands.push({ type, payload });
        return true;
      },
      flush: async () => {},
      getSnapshot: () => ({ status: 'saved' }),
    },
    canMutate: true,
    actionBusy: false,
    playhead: 1,
    selectedPassageId: 'passage',
    setPlayhead: () => {},
    runAction: async (action: () => Promise<unknown>) => {
      await action();
    },
  } as unknown as StudioContextValue;
  const platform = {
    filesystem: {
      saveFromUrl: async (_name: string, url: string) => {
        saves.push(url);
        return { status: 'saved' };
      },
    },
  } as unknown as Platform;
  globalThis.fetch = (async () => Response.json(current)) as typeof fetch;
  const tree = () => (
    <PlatformProvider platform={platform}>
      <StudioContext.Provider value={context}>
        <CaptionsPanel />
      </StudioContext.Provider>
    </PlatformProvider>
  );
  return {
    context,
    commands,
    saves,
    rerender: async () => {
      await act(async () => renderer!.update(tree()));
    },
    render: async (options?: Parameters<typeof create>[1]) => {
      await act(async () => {
        renderer = create(tree(), options);
      });
    },
  };
}

test('caption export is blocked when current audio invalidates prior review', async () => {
  const data = fixture({ ...status(), current: false, exportable: false });
  await data.render();
  expect(button('Export SRT').props.disabled).toBe(true);
  expect(button('Export WebVTT').props.disabled).toBe(true);
  expect(
    renderer!.root
      .findAllByProps({ role: 'alert' })
      .some((node) => text(node).includes('arrangement changed')),
  ).toBe(true);
  expect(data.saves).toHaveLength(0);
});

test('review of an earlier audio fingerprint cannot approve a changed arrangement', async () => {
  const remote = status();
  const data = fixture(remote);
  await data.render();
  const checkbox = () =>
    renderer!.root.findAllByType('input').find((node) => node.props.type === 'checkbox')!;
  act(() => checkbox().props.onChange({ target: { checked: true } }));
  expect(checkbox().props.checked).toBe(true);
  remote.source_fingerprint = 'b'.repeat(64);
  remote.current = false;
  remote.reviewed = false;
  remote.exportable = false;
  data.context.project.revision++;
  await data.rerender();
  expect(checkbox().props.checked).toBe(false);
  expect(button('Save caption draft').props.disabled).toBe(true);
  expect(data.commands).toHaveLength(0);
});

test('editing a cue revokes local review and saves explicit corrected sample timing', async () => {
  const data = fixture();
  await data.render();
  expect(button('Export SRT').props.disabled).toBe(false);
  const editor = renderer!.root.findByType('textarea');
  act(() => editor.props.onChange({ target: { value: 'A repaired phrase' } }));
  expect(button('Export SRT').props.disabled).toBe(true);
  const checkbox = renderer!.root
    .findAllByType('input')
    .find((node) => node.props.type === 'checkbox')!;
  act(() => checkbox.props.onChange({ target: { checked: true } }));
  const start = renderer!.root
    .findAllByType('input')
    .find((node) => node.props.value === 1 && node.props.step === 0.001)!;
  act(() => start.props.onChange({ target: { value: '1.25' } }));
  expect(
    renderer!.root.findAllByType('input').find((node) => node.props.type === 'checkbox')!.props
      .checked,
  ).toBe(false);
  act(() => checkbox.props.onChange({ target: { checked: true } }));
  await act(async () => {
    await button('Save reviewed captions').props.onClick();
  });
  expect(data.commands).toEqual([
    {
      type: 'set_captions',
      payload: {
        source_fingerprint: 'a'.repeat(64),
        reviewed: true,
        cues: [
          {
            id: 'cue',
            clip_id: null,
            start_sample: 60000,
            end_sample: 96000,
            text: 'A repaired phrase',
          },
        ],
      },
    },
  ]);
});

test('caption download rechecks server source and encodes its fingerprint', async () => {
  const data = fixture();
  await data.render();
  await act(async () => {
    await button('Export WebVTT').props.onClick();
  });
  expect(data.saves).toEqual([
    `http://server/studio/v1/projects/project/captions/export?format=vtt&source_fingerprint=${'a'.repeat(64)}`,
  ]);
  expect(renderer!.root.findByType('output').props.children).toBe('Captions saved.');
});

test('local reference videos revoke object URLs on replacement and unmount', async () => {
  const created: string[] = [],
    revoked: string[] = [];
  URL.createObjectURL = () => {
    const value = `blob:video-${created.length}`;
    created.push(value);
    return value;
  };
  URL.revokeObjectURL = (value) => {
    revoked.push(value);
  };
  const data = fixture();
  await data.render();
  const upload = renderer!.root.findAllByType('input').find((node) => node.props.type === 'file')!;
  act(() => upload.props.onChange({ currentTarget: { files: [{ name: 'film.mp4' }] } }));
  expect(renderer!.root.findByType('video').props.muted).toBe(true);
  act(() => upload.props.onChange({ currentTarget: { files: [{ name: 'film-2.mp4' }] } }));
  expect(revoked).toEqual(['blob:video-0']);
  await act(async () => {
    await button('Save video timing').props.onClick();
  });
  expect(data.commands[0]).toEqual({
    type: 'set_video_reference',
    payload: {
      reference: {
        name: 'film-2.mp4',
        frame_rate: 24,
        time_origin_sample: 0,
        duration_seconds: null,
      },
    },
  });
  act(() => renderer!.unmount());
  renderer = undefined;
  expect(revoked).toEqual(created);
});

test('caption transport detaches fetch receiver and rejects malformed status', async () => {
  let receiver: unknown = 'uninitialized';
  const client = new CaptionsClient('http://server/', async function (this: unknown) {
    receiver = this;
    return Response.json({ ...status(), exportable: 'yes' });
  } as typeof fetch);
  await expect(client.status('project/id')).rejects.toThrow('Invalid caption status');
  expect(receiver).toBeUndefined();
});

test('reference video follows timeline playback and stops when its panel closes', async () => {
  URL.createObjectURL = () => 'blob:timeline';
  URL.revokeObjectURL = () => {};
  let played = 0,
    paused = 0;
  const media = {
    currentTime: 0,
    play: async () => {
      played++;
    },
    pause: () => {
      paused++;
    },
  };
  const data = fixture(status(), 'timeline');
  await data.render({ createNodeMock: (element) => (element.type === 'video' ? media : null) });
  const upload = renderer!.root.findAllByType('input').find((node) => node.props.type === 'file')!;
  await act(async () =>
    upload.props.onChange({ currentTarget: { files: [{ name: 'film.mp4' }] } }),
  );
  expect(played).toBe(1);
  expect(media.currentTime).toBe(1);
  act(() => renderer!.unmount());
  renderer = undefined;
  expect(paused).toBe(1);
});

test('the caption dialog can start and stop narration without closing the reference video', async () => {
  const data = fixture();
  const calls: unknown[][] = [];
  let stops = 0;
  data.context.project.document.clips.push({ id: 'clip' } as never);
  data.context.playback.timeline = async (...args) => {
    calls.push(args);
  };
  data.context.playback.stop = () => {
    stops++;
  };
  data.context.client.assetAudio = (id) => `/assets/${id}`;
  await data.render();
  await act(async () => {
    await button('Play narration preview').props.onClick();
  });
  expect(calls).toHaveLength(1);
  expect(calls[0][0]).toBe(data.context.project.document);
  expect(calls[0][3]).toBe(data.context.playhead);
  data.context.playback.getSnapshot().mode = 'timeline';
  await data.rerender();
  await act(async () => {
    await button('Stop narration preview').props.onClick();
  });
  expect(stops).toBe(1);
});
