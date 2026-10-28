import { memo, useEffect, useState } from 'react';
import { type WaveformData, waveformRequest, waveformView } from './waveform-data';

export const Waveform = memo(function Waveform({
  url,
  trimStart,
  trimEnd,
  projectRate,
}: {
  url: string;
  trimStart: number;
  trimEnd: number;
  projectRate: number;
}) {
  const [result, setResult] = useState<{ url: string; data?: WaveformData; failed?: boolean }>({
    url,
  });
  useEffect(() => {
    let active = true;
    waveformRequest(url).then(
      (data) => {
        if (active) setResult({ url, data });
      },
      () => {
        if (active) setResult({ url, failed: true });
      },
    );
    return () => {
      active = false;
    };
  }, [url]);
  if (result.url !== url || (!result.data && !result.failed)) return null;
  if (result.failed || !result.data)
    return <span className="studio-waveform-unavailable">Source unavailable</span>;
  const { data } = result;
  const view = waveformView(
    data.frames,
    data.sample_rate,
    projectRate,
    trimStart,
    trimEnd,
    data.peaks.length,
  );
  if (view.width <= 0) return null;
  return (
    <svg
      className="studio-waveform"
      aria-hidden="true"
      viewBox={`${view.start} -1 ${view.width} 2`}
      preserveAspectRatio="none"
    >
      <path
        d={data.peaks.map(([low, high], index) => `M${index + 0.5} ${-high}V${-low}`).join('')}
        vectorEffect="non-scaling-stroke"
      />
    </svg>
  );
});
