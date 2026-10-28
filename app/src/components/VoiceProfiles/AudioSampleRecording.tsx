import { Mic, Pause, Play, Square } from 'lucide-react';
import { memo, useEffect, useId, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Visualizer } from 'react-sound-visualizer';
import { Button } from '@/components/ui/button';
import { Checkbox } from '@/components/ui/checkbox';
import { useReferenceCaptureStore } from '@/stores/referenceCaptureStore';
import { FormControl, FormItem, FormMessage } from '@/components/ui/form';
import { formatAudioDuration } from '@/lib/utils/audio';

const MemoizedWaveform = memo(function MemoizedWaveform({
  audioStream,
}: {
  audioStream: MediaStream;
}) {
  return (
    <div className="absolute inset-0 pointer-events-none flex items-center justify-center opacity-30">
      <Visualizer audio={audioStream} autoStart strokeColor="#b39a3d">
        {({ canvasRef }) => (
          <canvas ref={canvasRef} width={500} height={150} className="w-full h-full" />
        )}
      </Visualizer>
    </div>
  );
});

interface AudioSampleRecordingProps {
  file: File | null | undefined;
  isRecording: boolean;
  recordingStream?: MediaStream | null;
  duration: number;
  onStart: () => void;
  onStop: () => void;
  onCancel: () => void;
  onTranscribe: () => void;
  onPlayPause: () => void;
  isPlaying: boolean;
  isTranscribing?: boolean;
  showWaveform?: boolean;
}

export function AudioSampleRecording({
  file,
  isRecording,
  recordingStream,
  duration,
  onStart,
  onStop,
  onCancel,
  onTranscribe,
  onPlayPause,
  isPlaying,
  isTranscribing = false,
  showWaveform = true,
}: AudioSampleRecordingProps) {
  const { t } = useTranslation();
  const cleanReferenceAudio = useReferenceCaptureStore((state) => state.cleanReferenceAudio);
  const setCleanReferenceAudio = useReferenceCaptureStore((state) => state.setCleanReferenceAudio);
  const processingId = useId();
  const [audioStream, setAudioStream] = useState<MediaStream | null>(null);

  const waveformStream = isRecording ? recordingStream : audioStream;

  // Preview uses the same requested processing policy as reference recording.
  useEffect(() => {
    if (!showWaveform || isRecording || file) return;
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) return;

    let stream: MediaStream | null = null;
    let disposed = false;

    navigator.mediaDevices
      .getUserMedia({ audio: {
        echoCancellation: !cleanReferenceAudio,
        noiseSuppression: !cleanReferenceAudio,
        autoGainControl: !cleanReferenceAudio,
      }, video: false })
      .then((s) => {
        if (disposed) {
          s.getTracks().forEach((track) => track.stop());
          return;
        }
        stream = s;
        setAudioStream(s);
      })
      .catch((err) => {
        console.warn('Could not access microphone for visualization:', err);
      });

    return () => {
      disposed = true;
      setAudioStream(null);
      if (stream) {
        stream.getTracks().forEach((track) => {
          track.stop();
        });
      }
    };
  }, [showWaveform, isRecording, file, cleanReferenceAudio]);

  return (
    <FormItem>
      <FormControl>
        <div className="space-y-4">
          {!file && (
            <div className="space-y-1">
              <label htmlFor={processingId} className="flex items-center gap-2 text-sm">
                <Checkbox
                  id={processingId}
                  checked={cleanReferenceAudio}
                  onCheckedChange={(checked) => setCleanReferenceAudio(checked === true)}
                  disabled={isRecording}
                />
                {t('audioSample.disableProcessing', 'Disable browser audio processing')}
              </label>
              <p className="text-xs text-muted-foreground">
                {t(
                  'audioSample.disableProcessingHint',
                  'For reference recordings only. Requests no noise suppression, echo cancellation, or automatic gain. Recordings may still use compressed audio.',
                )}
              </p>
            </div>
          )}
          {!isRecording && !file && (
            <div className="relative flex flex-col items-center justify-center gap-4 p-4 border-2 border-dashed rounded-lg min-h-[180px] overflow-hidden">
              {showWaveform && waveformStream && <MemoizedWaveform audioStream={waveformStream} />}
              <Button
                type="button"
                onClick={onStart}
                size="lg"
                className="relative z-10 flex items-center gap-2"
              >
                <Mic className="h-5 w-5" />
                {t('audioSample.startRecording')}
              </Button>
              <p className="relative z-10 text-sm text-muted-foreground text-center">
                {t('audioSample.recordHint')}
              </p>
            </div>
          )}

          {isRecording && (
            <div className="relative flex flex-col items-center justify-center gap-4 p-4 border-2 border-accent rounded-lg bg-accent/5 min-h-[180px] overflow-hidden">
              {showWaveform && waveformStream && <MemoizedWaveform audioStream={waveformStream} />}
              <div className="relative z-10 flex items-center gap-4">
                <div className="flex items-center gap-2">
                  <div className="h-3 w-3 rounded-full bg-accent animate-pulse" />
                  <span className="text-lg font-mono font-semibold">
                    {formatAudioDuration(duration)}
                  </span>
                </div>
              </div>
              <Button
                type="button"
                onClick={onStop}
                className="relative z-10 flex items-center gap-2 bg-accent text-accent-foreground hover:bg-accent/90"
              >
                <Square className="h-4 w-4" />
                {t('audioSample.stopRecording')}
              </Button>
              <p className="relative z-10 text-sm text-muted-foreground text-center">
                {t('audioSample.remaining', { time: formatAudioDuration(30 - duration) })}
              </p>
            </div>
          )}

          {file && !isRecording && (
            <div className="flex flex-col items-center justify-center gap-4 p-4 border-2 border-primary rounded-lg bg-primary/5 min-h-[180px]">
              <div className="flex items-center gap-2">
                <Mic className="h-5 w-5 text-primary" />
                <span className="font-medium">{t('audioSample.recordingComplete')}</span>
              </div>
              <p className="text-sm text-muted-foreground text-center">
                {t('audioSample.fileLabel', { name: file.name })}
              </p>
              <div className="flex gap-2">
                <Button
                  type="button"
                  size="icon"
                  variant="outline"
                  onClick={onPlayPause}
                  aria-label={isPlaying ? t('audioSample.pause') : t('audioSample.play')}
                >
                  {isPlaying ? <Pause className="h-4 w-4" /> : <Play className="h-4 w-4" />}
                </Button>
                <Button
                  type="button"
                  variant="outline"
                  onClick={onTranscribe}
                  disabled={isTranscribing}
                  className="flex items-center gap-2"
                >
                  <Mic className="h-4 w-4" />
                  {isTranscribing ? t('audioSample.transcribing') : t('audioSample.transcribe')}
                </Button>
                <Button
                  type="button"
                  variant="outline"
                  onClick={onCancel}
                  className="flex items-center gap-2"
                >
                  {t('audioSample.recordAgain')}
                </Button>
              </div>
            </div>
          )}
        </div>
      </FormControl>
      <FormMessage />
    </FormItem>
  );
}
