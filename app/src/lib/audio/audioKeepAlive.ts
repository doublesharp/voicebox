/** Keep WKWebView's media session open without repeatedly seeking a short file. */
export function createAudioKeepAlive() {
  const context = new AudioContext();
  const source = context.createConstantSource();
  const destination = context.createMediaStreamDestination();
  // True PCM silence, not a muted element (which WebKit can optimize away).
  source.offset.value = 0;
  source.connect(destination);
  source.start();

  const audio = new Audio();
  audio.srcObject = destination.stream;
  audio.volume = 1;
  let disposed = false;

  return {
    async start(): Promise<void> {
      if (disposed) return;
      // Issue both requests inside the gesture. Waiting for resume() first can
      // lose autoplay permission; a pending resume must also allow later retries.
      const resumed = context.state === 'running' ? Promise.resolve() : context.resume();
      const playing = audio.paused ? audio.play() : Promise.resolve();
      await Promise.all([resumed, playing]);
    },
    async dispose(): Promise<void> {
      if (disposed) return;
      disposed = true;
      audio.pause();
      audio.srcObject = null;
      source.stop();
      source.disconnect();
      destination.stream.getTracks().forEach((track) => {
        track.stop();
      });
      await context.close();
    },
  };
}
