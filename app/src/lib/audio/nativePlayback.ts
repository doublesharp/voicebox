import type {
  NativePlaybackControl,
  NativePlaybackEvent,
  NativePlaybackSession,
  PlatformAudio,
} from '@/platform/types';
import { legacyPlaybackToken, prepareLegacy, startNative } from './playbackOwnership';

export interface NativePlaybackUpdate extends Omit<NativePlaybackEvent, 'session_id' | 'state'> {
  session_id: string | null;
  state: NativePlaybackEvent['state'] | 'starting';
}

/** Owns the pending request as well as the native session it eventually starts.
 * Session-scoped stops ensure an old async completion cannot stop its replacement.
 */
export class NativePlaybackController {
  private generation = 0;
  private session: NativePlaybackSession | null = null;
  private abort: AbortController | null = null;
  private subscription: Promise<void> | null = null;
  private unlisten: (() => void) | null = null;
  private disposed = false;
  private starting = false;
  private pendingControl: NativePlaybackControl = {};
  private pendingEvents = new Map<string, NativePlaybackEvent>();
  private releaseOwnership: (() => void) | null = null;
  private pendingLaunch: Promise<void> | null = null;

  constructor(
    private readonly audio: PlatformAudio,
    private readonly onUpdate: (event: NativePlaybackUpdate) => void,
    private readonly fetchAudio: (url: string, init?: RequestInit) => Promise<Response> = fetch,
  ) {}

  get isStarting(): boolean {
    return this.starting;
  }
  get hasSession(): boolean {
    return this.session !== null;
  }

  private subscribe(): Promise<void> {
    if (!this.subscription) {
      this.subscription = this.audio
        .subscribeToPlayback((event) => {
          if (this.disposed) return;
          if (this.session?.session_id === event.session_id) {
            this.apply(event);
          } else if (this.starting) {
            // Even a very short clip can end before its invoke promise resolves.
            this.pendingEvents.set(event.session_id, event);
            if (this.pendingEvents.size > 8)
              this.pendingEvents.delete(this.pendingEvents.keys().next().value!);
          }
        })
        .then((unlisten) => {
          if (this.disposed) unlisten();
          else this.unlisten = unlisten;
        })
        .catch((error) => {
          this.subscription = null;
          throw error;
        });
    }
    return this.subscription;
  }

  private apply(event: NativePlaybackEvent): void {
    if (this.session) this.session = { ...this.session, position: event.position };
    if (event.state === 'ended' || event.state === 'stopped' || event.state === 'error') {
      this.session = null;
      this.releaseOwnership?.();
    }
    this.onUpdate(event);
  }

  async start(url: string, deviceIds: string[], position: number, volume: number): Promise<void> {
    if (this.disposed) return;
    const generation = ++this.generation;
    const previous = this.session;
    this.session = null;
    this.releaseOwnership?.();
    this.abort?.abort();
    const token = legacyPlaybackToken();
    const abort = new AbortController();
    this.abort = abort;
    this.starting = true;
    this.pendingEvents.clear();
    this.pendingControl = {};
    const valid = () => generation === this.generation && !this.disposed && token.valid();
    const releaseOwnership = () => {
      token.signal.removeEventListener('abort', revoked);
      token.release();
      if (this.releaseOwnership === releaseOwnership) this.releaseOwnership = null;
    };
    const revoked = () => {
      abort.abort();
      if (generation !== this.generation) return;
      ++this.generation;
      const session = this.session;
      this.session = null;
      this.abort = null;
      this.starting = false;
      this.pendingEvents.clear();
      this.pendingControl = {};
      releaseOwnership();
      this.onUpdate({
        session_id: session?.session_id ?? null,
        state: 'stopped',
        position: session?.position ?? position,
        duration: session?.duration ?? 0,
      });
    };
    token.signal.addEventListener('abort', revoked, { once: true });
    this.releaseOwnership = releaseOwnership;
    const launched: { session: NativePlaybackSession | null } = { session: null };
    this.onUpdate({ session_id: null, state: 'starting', position, duration: 0 });
    try {
      // The queue includes outstanding invokes from other players. The native
      // stop with no session also revokes a different controller's active output.
      await prepareLegacy(token, () => this.audio.stopPlayback(previous?.session_id));
      if (!valid()) return;
      await this.subscribe();
      if (!valid()) return;
      const response = await this.fetchAudio(url, { signal: abort.signal });
      if (!response.ok) throw new Error(`Unable to load audio (${response.status})`);
      const data = new Uint8Array(await response.arrayBuffer());
      if (!valid()) return;
      const launch = startNative(
        token,
        async () => {
          const session = await this.audio.playToDevices(data, [...new Set(deviceIds)], {
            startTime: position,
            volume,
          });
          launched.session = session;
          if (!valid()) return;
          let applied: NativePlaybackControl = {};
          while (Object.keys(this.pendingControl).length > 0) {
            const queued = this.pendingControl;
            this.pendingControl = {};
            await this.audio.controlPlayback(session.session_id, queued);
            applied = { ...applied, ...queued };
            if (!valid()) return;
          }
          this.session = session;
          this.starting = false;
          const latest = this.pendingEvents.get(session.session_id) ?? {
            ...session,
            state: 'playing' as const,
          };
          this.pendingEvents.clear();
          const terminal =
            latest.state === 'ended' || latest.state === 'stopped' || latest.state === 'error';
          this.apply(
            terminal
              ? latest
              : {
                  ...latest,
                  position: applied.position ?? latest.position,
                  state:
                    applied.paused === undefined
                      ? latest.state
                      : applied.paused
                        ? 'paused'
                        : 'playing',
                },
          );
        },
        async () => {
          if (launched.session) await this.audio.stopPlayback(launched.session.session_id);
        },
      );
      this.pendingLaunch = launch;
      try {
        await launch;
      } finally {
        if (this.pendingLaunch === launch) this.pendingLaunch = null;
      }
    } catch (error) {
      if (launched.session)
        await this.audio.stopPlayback(launched.session.session_id).catch(() => {});
      if (valid()) {
        this.session = null;
        this.starting = false;
        this.onUpdate({
          session_id: null,
          state: 'error',
          position,
          duration: 0,
          error: error instanceof Error ? error.message : String(error),
        });
      }
    } finally {
      if (generation === this.generation) {
        this.starting = false;
        this.abort = null;
        if (!this.session) releaseOwnership();
      }
    }
  }

  async control(control: NativePlaybackControl): Promise<void> {
    const session = this.session;
    if (this.disposed) return;
    if (this.starting) {
      this.pendingControl = { ...this.pendingControl, ...control };
      return;
    }
    if (!session) return;
    await this.audio.controlPlayback(session.session_id, control);
  }

  async stop(): Promise<void> {
    ++this.generation;
    this.releaseOwnership?.();
    this.abort?.abort();
    this.abort = null;
    this.starting = false;
    this.pendingEvents.clear();
    this.pendingControl = {};
    const session = this.session;
    this.session = null;
    if (session) await this.audio.stopPlayback(session.session_id);
    await this.pendingLaunch;
  }

  dispose(): void {
    this.disposed = true;
    void this.stop().catch(() => {});
    this.unlisten?.();
    this.unlisten = null;
  }
}
