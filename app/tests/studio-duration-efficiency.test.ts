import { expect, test } from 'bun:test';
import { timelineEnd } from '../src/features/studio/domain';
import type { StudioAsset, StudioDocument } from '../src/features/studio/types';

test('timeline duration visits assets once while respecting rates and source trims', () => {
  let lookups = 0;
  const assets = Array.from({ length: 500 }, (_, i) => ({
    get id() {
      lookups++;
      return String(i);
    },
    frames: 48000,
    sample_rate: 24000,
  })) as StudioAsset[];
  const document = {
    sample_rate: 48000,
    clips: Array.from({ length: 500 }, (_, i) => ({
      asset_id: String(i),
      start_sample: i * 48000,
      trim_start_sample: 12000,
      trim_end_sample: 24000,
    })),
  } as StudioDocument;
  expect(timelineEnd(document, assets)).toBe(499 * 48000 + 60000);
  expect(lookups).toBeLessThanOrEqual(1000);
});
