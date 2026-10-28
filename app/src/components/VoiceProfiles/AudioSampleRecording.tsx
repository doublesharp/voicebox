import { Mic, Pause, Play, Square } from 'lucide-react';
import { memo } from 'react';
import { useTranslation } from 'react-i18next';
import { Visualizer } from 'react-sound-visualizer';
import { Button } from '@/components/ui/button';
import { MicrophoneSelector } from '@/components/MicrophoneSelector/MicrophoneSelector';
import { useMicrophonePreview } from '@/lib/hooks/useMicrophonePreview';
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
  isStarting?: boolean;
  recordingStream?: MediaStream | null;
  usingDefaultMicrophone?: boolean;
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
  isStarting = false,
  recordingStream,
  usingDefaultMicrophone = false,
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
  const preview = useMicrophonePreview(showWaveform && !file && !isRecording && !isStarting);
  const audioStream = isRecording ? recordingStream : preview.stream;

  return (
    <FormItem>
      <FormControl>
        <div className="space-y-4">
          <MicrophoneSelector disabled={isRecording || isStarting} />
          {(usingDefaultMicrophone || preview.usingDefaultMicrophone) && (
            <output className="block text-sm text-muted-foreground">
              {t('microphone.fallbackNotice')}
            </output>
          )}
          {preview.error && (
            <p role="alert" className="text-sm text-destructive">
              {preview.error}
            </p>
          )}
          {!isRecording && !file && (
            <div className="relative flex flex-col items-center justify-center gap-4 p-4 border-2 border-dashed rounded-lg min-h-[180px] overflow-hidden">
              {showWaveform && audioStream && <MemoizedWaveform audioStream={audioStream} />}
              <Button
                type="button"
                onClick={onStart}
                disabled={isStarting}
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
              {showWaveform && audioStream && <MemoizedWaveform audioStream={audioStream} />}
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
