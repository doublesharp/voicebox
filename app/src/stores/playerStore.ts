import { create } from 'zustand';

interface PlayerState {
  sourceGeneration: number;
  audioUrl: string | null;
  audioId: string | null;
  profileId: string | null;
  title: string | null;
  isPlaying: boolean;
  currentTime: number;
  duration: number;
  volume: number;
  isLooping: boolean;
  shouldRestart: boolean;
  shouldAutoPlay: boolean;
  onFinish: (() => void) | null;

  setAudio: (url: string, id: string, profileId: string | null, title?: string) => void;
  setAudioWithAutoPlay: (url: string, id: string, profileId: string | null, title?: string) => void;
  setIsPlaying: (playing: boolean) => void;
  setCurrentTime: (time: number) => void;
  setDuration: (duration: number) => void;
  setVolume: (volume: number) => void;
  toggleLoop: () => void;
  restartCurrentAudio: () => void;
  clearRestartFlag: () => void;
  clearAutoPlayFlag: () => void;
  setOnFinish: (callback: (() => void) | null) => void;
  reset: () => void;
}

export const usePlayerStore = create<PlayerState>((set) => ({
  sourceGeneration: 0,
  audioUrl: null,
  audioId: null,
  profileId: null,
  title: null,
  isPlaying: false,
  currentTime: 0,
  duration: 0,
  volume: 1,
  isLooping: false,
  shouldRestart: false,
  shouldAutoPlay: false,
  onFinish: null,

  setAudio: (url, id, profileId, title) =>
    set((state) => ({
      sourceGeneration: state.sourceGeneration + 1,
      audioUrl: url,
      audioId: id,
      profileId: profileId || null,
      title: title || null,
      currentTime: 0,
      duration: 0,
      isPlaying: false,
      shouldRestart: false,
      shouldAutoPlay: false,
    })),
  setAudioWithAutoPlay: (url, id, profileId, title) =>
    set((state) => ({
      sourceGeneration: state.sourceGeneration + 1,
      audioUrl: url,
      audioId: id,
      profileId: profileId || null,
      title: title || null,
      currentTime: 0,
      duration: 0,
      isPlaying: false,
      shouldRestart: false,
      shouldAutoPlay: true,
    })),
  setIsPlaying: (playing) => set({ isPlaying: playing }),
  setCurrentTime: (time) => set({ currentTime: time }),
  setDuration: (duration) => set({ duration }),
  setVolume: (volume) => set({ volume }),
  toggleLoop: () => set((state) => ({ isLooping: !state.isLooping })),
  restartCurrentAudio: () => set({ shouldRestart: true }),
  clearRestartFlag: () => set({ shouldRestart: false }),
  clearAutoPlayFlag: () => set({ shouldAutoPlay: false }),
  setOnFinish: (callback) => set({ onFinish: callback }),
  reset: () =>
    set((state) => ({
      sourceGeneration: state.sourceGeneration + 1,
      audioUrl: null,
      audioId: null,
      profileId: null,
      title: null,
      isPlaying: false,
      currentTime: 0,
      duration: 0,
      isLooping: false,
      shouldRestart: false,
      shouldAutoPlay: false,
      onFinish: null,
    })),
}));
