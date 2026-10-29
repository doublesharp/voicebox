import { expect, test } from 'bun:test';
import { act, create, type ReactTestRenderer } from 'react-test-renderer';
import { StudioContext, type StudioContextValue } from '../src/features/studio/context';
import { AudioMeter } from '../src/features/studio/meters';
import { StudioTransport } from '../src/features/studio/StudioTransport';

test('transport clock does not revisit assets; a changed source revision updates duration', async () => {
  const listeners = new Set<() => void>();
  let lookups = 0;
  let clock = { mode: 'timeline', label: 'Timeline', position: 0, duration: 2, error: null };
  const asset = {
    get id() {
      lookups++;
      return 'asset';
    },
    frames: 48000,
    sample_rate: 24000,
  };
  const context = {
    playback: {
      meters: new AudioMeter(),
      subscribe: (listener: () => void) => {
        listeners.add(listener);
        return () => {
          listeners.delete(listener);
        };
      },
      getSnapshot: () => clock,
      getPlayhead: () => clock.position,
    },
    project: {
      assets: [asset],
      document: {
        sample_rate: 48000,
        clips: [{ asset_id: 'asset', start_sample: 0, trim_start_sample: 0, trim_end_sample: 0 }],
      },
    },
    setPlayhead() {},
  } as unknown as StudioContextValue;
  let renderer!: ReactTestRenderer;
  try {
    await act(async () => {
      renderer = create(
        <StudioContext.Provider value={context}>
          <StudioTransport toggleTransport={() => {}} />
        </StudioContext.Provider>,
      );
    });
    const initial = lookups;
    act(() => {
      clock = { ...clock, position: 0.8 };
      for (const listener of listeners) listener();
    });
    expect(renderer.root.findByProps({ 'aria-label': 'Playhead seconds' }).props.value).toBe(0.8);
    expect(lookups).toBe(initial);
    const updated = {
      ...context,
      project: { ...context.project, assets: [{ ...asset, frames: 96000 }] },
    };
    await act(async () =>
      renderer.update(
        <StudioContext.Provider value={updated}>
          <StudioTransport toggleTransport={() => {}} />
        </StudioContext.Provider>,
      ),
    );
    expect(JSON.stringify(renderer.toJSON())).toContain('0:04.0');
  } finally {
    act(() => renderer?.unmount());
  }
});
