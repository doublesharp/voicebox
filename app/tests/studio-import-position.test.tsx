import { expect, test } from 'bun:test';
import { act, create, type ReactTestRenderer } from 'react-test-renderer';
import { StudioContext, type StudioContextValue } from '../src/features/studio/context';
import { Timeline } from '../src/features/studio/Timeline';

test('imported media retains the insertion cursor chosen before a slow upload', async () => {
  let cursor = 0.5;
  let finishUpload!: (asset: { id: string }) => void;
  let action: Promise<unknown> | undefined;
  const commands: unknown[] = [];
  const context = {
    project: {
      id: 'project',
      assets: [],
      document: { sample_rate: 48000, tracks: [], clips: [], passages: [] },
    },
    client: {
      uploadAsset: () =>
        new Promise((resolve) => {
          finishUpload = resolve;
        }),
    },
    playback: { subscribe: () => () => {}, getPlayhead: () => cursor },
    session: {
      command: async (type: string, payload: unknown) => {
        commands.push([type, payload]);
      },
    },
    canMutate: true,
    checkedPassages: [],
    runAction: (work: () => Promise<unknown>) => {
      action = work();
      return action;
    },
  } as unknown as StudioContextValue;
  let view!: ReactTestRenderer;
  try {
    await act(async () => {
      view = create(
        <StudioContext.Provider value={context}>
          <Timeline />
        </StudioContext.Provider>,
      );
    });
    act(() =>
      view.root
        .findAllByType('input')
        .find((node) => node.props['aria-label'] === 'Import arrangement audio')!
        .props.onChange({
          target: { files: [new File(['sound'], 'music.wav')], value: 'music.wav' },
        }),
    );
    cursor = 3;
    await act(async () => {
      finishUpload({ id: 'asset' });
      await action;
    });
    expect(commands).toEqual([
      [
        'insert_asset',
        { asset_id: 'asset', start_sample: 24000, kind: 'music', name: 'music.wav' },
      ],
    ]);
  } finally {
    act(() => view?.unmount());
  }
});
