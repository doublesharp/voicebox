/** Serializes native output transitions and invalidates pending legacy starts. */
let generation = 0;
let pending: Promise<void> = Promise.resolve();
const aborters = new Set<AbortController>();
export function revokePlayback() {
  generation++;
  for (const controller of aborters) controller.abort();
  aborters.clear();
}
export function legacyPlaybackToken() {
  const token = ++generation;
  const controller = new AbortController();
  aborters.add(controller);
  return {
    signal: controller.signal,
    valid: () => token === generation,
    release: () => aborters.delete(controller),
  };
}
export function stopNative(stop: () => void | Promise<void>): Promise<void> {
  revokePlayback();
  const stopping = pending.catch(() => {}).then(() => stop());
  pending = stopping;
  return stopping;
}
export async function startNative(
  token: ReturnType<typeof legacyPlaybackToken>,
  start: () => Promise<void>,
  stop: () => void | Promise<void>,
) {
  const starting = pending
    .catch(() => {})
    .then(async () => {
      if (!token.valid()) return;
      try {
        await start();
      } finally {
        if (!token.valid()) await stop();
      }
    });
  pending = starting;
  await starting;
}

export async function prepareLegacy(
  token: ReturnType<typeof legacyPlaybackToken>,
  stop: () => void | Promise<void>,
) {
  const stopping = pending
    .catch(() => {})
    .then(async () => {
      if (token.valid()) await stop();
    });
  pending = stopping;
  await stopping;
}
