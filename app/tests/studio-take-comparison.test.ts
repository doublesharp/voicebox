import { expect, test } from 'bun:test';
import { listeningGains, replacementNeedsReset } from '../src/features/studio/take-comparison';
import type { AuditionLevel, StudioProject } from '../src/features/studio/types';

const level = (rms: number, peak = rms): AuditionLevel => ({
  method: 'sample_rms_v1',
  status: rms ? 'measured' : 'silence',
  rms,
  peak,
  frames: 480000,
  measured_frames: 240000,
  sample_rate: 48000,
  channels: 1,
});
test('matches quieter sampled RMS using attenuation with sample-peak headroom', () => {
  const result = listeningGains(level(0.4, 0.8), level(0.1, 0.4));
  expect(result.gains).toEqual([0.25, 1]);
  const hot = listeningGains(level(0.4, 2), level(0.1, 1.2));
  expect(hot.gains![0] * 0.4).toBeCloseTo(hot.gains![1] * 0.1);
  expect(hot.gains![1] * 1.2).toBeLessThanOrEqual(10 ** (-1 / 20));
});
test('silence, incompatible layouts and invalid measurements cannot enable matching', () => {
  expect(listeningGains(level(0), level(0.1)).gains).toBeNull();
  expect(listeningGains({ ...level(0.1), channels: 2 }, level(0.1)).gains).toBeNull();
  expect(listeningGains({ ...level(0.1), rms: Number.NaN }, level(0.1)).gains).toBeNull();
});
test('replacement checks resampled sample ranges and fades without changing the project', () => {
  const project = {
    document: {
      sample_rate: 48000,
      clips: [
        {
          passage_id: 'p',
          trim_start_sample: 15000,
          trim_end_sample: 9000,
          fade_in_samples: 0,
          fade_out_samples: 0,
        },
      ],
    },
    assets: [{ id: 'new', frames: 12000, sample_rate: 24000 }],
  } as StudioProject;
  const before = JSON.stringify(project);
  expect(replacementNeedsReset(project, 'p', 'new')).toBe(true);
  project.document.clips[0].trim_end_sample = 8999;
  expect(replacementNeedsReset(project, 'p', 'new')).toBe(false);
  project.document.clips[0].fade_in_samples = 2;
  expect(replacementNeedsReset(project, 'p', 'new')).toBe(true);
  project.document.clips[0].trim_end_sample = 9000;
  project.document.clips[0].fade_in_samples = 0;
  expect(JSON.stringify(project)).toBe(before);
});
