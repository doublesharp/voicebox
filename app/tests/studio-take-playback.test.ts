import { afterEach, expect, test } from 'bun:test';
import { StudioPlayback } from '../src/features/studio/playback';

const OriginalAudio = globalThis.Audio;
const OriginalContext = globalThis.AudioContext;
let player: StudioPlayback;
class TestAudio {
  static instances: TestAudio[] = [];
  volume = 1;
  paused = true;
  onpause: (() => void) | null = null;
  async play() {
    this.paused = false;
  }
  pause() {
    this.paused = true;
  }
  removeAttribute() {}
  load() {}
  constructor() {
    TestAudio.instances.push(this);
  }
}
afterEach(() => {
  player?.dispose();
  globalThis.Audio = OriginalAudio;
  globalThis.AudioContext = OriginalContext;
});
test('audition attenuation is temporary, bounded and resets for an ordinary audition', async () => {
  globalThis.Audio = TestAudio as unknown as typeof Audio;
  globalThis.AudioContext = class {
    constructor() {
      throw new Error('No analysis');
    }
  } as unknown as typeof AudioContext;
  player = new StudioPlayback();
  await player.audition('/a', 'A', 0.25);
  expect(TestAudio.instances[0].volume).toBe(0.25);
  await player.audition('/b', 'B');
  expect(TestAudio.instances[0].paused).toBe(true);
  expect(TestAudio.instances[1].volume).toBe(1);
  await player.audition('/c', 'C', 3);
  expect(TestAudio.instances[2].volume).toBe(1);
  await player.audition('/d', 'D', Number.NaN);
  expect(TestAudio.instances[3].volume).toBe(1);
});
