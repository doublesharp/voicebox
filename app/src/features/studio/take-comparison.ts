import type { AuditionLevel, StudioProject } from './types';

export function listeningGains(
  a: AuditionLevel,
  b: AuditionLevel,
): {
  gains: [number, number] | null;
  reason: string;
} {
  if ([a, b].some((level) => level.status === 'silence'))
    return { gains: null, reason: 'A sampled source is silent. Matching is unavailable.' };
  if (
    [a, b].some(
      (level) =>
        level.method !== 'sample_rms_v1' ||
        level.status !== 'measured' ||
        !Number.isFinite(level.rms) ||
        level.rms <= 0 ||
        !Number.isFinite(level.peak) ||
        level.peak < level.rms ||
        ![1, 2].includes(level.channels) ||
        !Number.isInteger(level.measured_frames) ||
        level.measured_frames <= 0 ||
        level.measured_frames > Math.min(level.frames, 240000),
    )
  )
    return { gains: null, reason: 'Source listening level is not measurable.' };
  if (a.channels !== b.channels)
    return { gains: null, reason: 'Source channel layouts differ. Matching is unavailable.' };
  // Quieter sampled RMS, reduced further if needed for 1 dB sampled-peak headroom.
  const target = Math.min(a.rms, b.rms);
  const headroom = 10 ** (-1 / 20);
  const scale = Math.min(
    1,
    headroom / ((a.peak * target) / a.rms),
    headroom / ((b.peak * target) / b.rms),
  );
  return { gains: [(scale * target) / a.rms, (scale * target) / b.rms], reason: '' };
}

export function replacementNeedsReset(project: StudioProject, passageId: string, assetId: string) {
  const asset = project.assets.find((item) => item.id === assetId);
  if (!asset) return true;
  const frames = Math.round((asset.frames * project.document.sample_rate) / asset.sample_rate);
  return project.document.clips.some((clip) => {
    if (clip.passage_id !== passageId) return false;
    const length = frames - clip.trim_start_sample - clip.trim_end_sample;
    return length <= 0 || clip.fade_in_samples > length || clip.fade_out_samples > length;
  });
}
