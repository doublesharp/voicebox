import { afterAll, afterEach, describe, expect, mock, test } from 'bun:test';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { createElement, Fragment, type ReactNode } from 'react';
import { act, create, type ReactTestRenderer } from 'react-test-renderer';
import type { GenerationVersionResponse, HistoryResponse } from '../src/lib/api/types';
import { useGenerationStore } from '../src/stores/generationStore';
import { usePlayerStore } from '../src/stores/playerStore';

const { apiClient: actualApiClient } = await import('../src/lib/api/client');

const original: GenerationVersionResponse = {
  id: 'original',
  generation_id: 'gen',
  label: 'original',
  audio_path: 'original.wav',
  is_default: false,
  created_at: '2026-10-28T10:00:00Z',
};
const processed: GenerationVersionResponse = {
  ...original,
  id: 'processed',
  label: 'version-2',
  audio_path: 'processed.wav',
  source_version_id: 'original',
  is_default: true,
  effects_chain: [{ type: 'gain', enabled: true, params: { gain_db: -6 } }],
};
const generation = {
  id: 'gen',
  profile_id: 'voice',
  profile_name: 'Voice',
  text: 'Hello',
  language: 'en',
  status: 'completed',
  created_at: original.created_at,
  versions: [original, processed],
} as HistoryResponse;
let historyData = { items: [generation], total: 1 };
let laterPage: typeof historyData | undefined;
let intersect: (() => void) | undefined;
const mutation = { mutate: mock(() => {}), isPending: false, isSuccess: false };
const deleteMutation = { ...mutation };
const clearMutation = { ...mutation };
const importMutation = { ...mutation };
const update = mock(
  async (_generation: string, _id: string, data: { effects_chain: unknown[] }) => ({
    ...processed,
    audio_path: 'updated.wav',
    effects_chain: data.effects_chain,
  }),
);
const remove = mock(async () => [{ ...original, is_default: true }]);
const setDefault = mock(async () => ({ ...original, is_default: true }));
const toast = mock(() => {});

mock.module('../src/lib/hooks/useHistory', () => ({
  useHistory: ({ offset }: { offset: number }) => ({
    data: offset === 0 ? historyData : laterPage,
    isLoading: false,
    isFetching: false,
  }),
  useDeleteGeneration: () => deleteMutation,
  useClearFailedGenerations: () => clearMutation,
  useExportGeneration: () => mutation,
  useExportGenerationAudio: () => mutation,
  useImportGeneration: () => importMutation,
}));
mock.module('../src/lib/api/client', () => ({
  apiClient: {
    updateGenerationVersion: update,
    deleteGenerationVersion: remove,
    getVersionAudioUrl: (id: string, revision: string) =>
      `/audio/version/${id}?revision=${revision}`,
    getAudioUrl: (id: string) => `/audio/${id}`,
    setDefaultVersion: setDefault,
  },
}));
mock.module('react-i18next', () => ({
  useTranslation: () => ({ t: (key: string) => key }),
  initReactI18next: { type: '3rdParty', init() {} },
}));
mock.module('../src/components/ui/use-toast', () => ({ useToast: () => ({ toast }) }));
mock.module('framer-motion', () => ({ AnimatePresence: Fragment, motion: { div: 'div' } }));
mock.module('../src/components/Effects/EffectsChainEditor', () => ({
  EffectsChainEditor: ({
    value,
    onChange,
  }: {
    value: unknown[];
    onChange: (chain: unknown[]) => void;
  }) => (
    <div>
      <span data-chain>{JSON.stringify(value)}</span>
      <button type="button" onClick={() => onChange([])}>
        Clear effects
      </button>
    </div>
  ),
}));
const container = ({ children }: { children: ReactNode }) => <div>{children}</div>;
mock.module('../src/components/ui/dialog', () => ({
  Dialog: ({ open, children }: { open: boolean; children: ReactNode }) =>
    open ? <div>{children}</div> : null,
  DialogContent: container,
  DialogHeader: container,
  DialogTitle: container,
  DialogDescription: container,
  DialogFooter: container,
}));
mock.module('../src/components/ui/dropdown-menu', () => ({
  DropdownMenu: container,
  DropdownMenuContent: container,
  DropdownMenuTrigger: container,
  DropdownMenuItem: ({
    children,
    disabled,
    onSelect,
  }: {
    children: ReactNode;
    disabled?: boolean;
    onSelect?: () => void;
  }) => (
    <button type="button" disabled={disabled} onClick={onSelect}>
      {children}
    </button>
  ),
}));
Object.defineProperty(globalThis, 'IntersectionObserver', {
  configurable: true,
  value: class {
    constructor(callback: (entries: { isIntersecting: boolean }[]) => void) {
      intersect = () => callback([{ isIntersecting: true }]);
    }
    observe() {}
    disconnect() {}
  },
});

const { HistoryTable } = await import('../src/components/History/HistoryTable');
let renderer: ReactTestRenderer;
let client: QueryClient;
function mount(items = [generation], total = items.length) {
  historyData = { items, total };
  laterPage = undefined;
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  act(() => {
    renderer = create(createElement(QueryClientProvider, { client }, createElement(HistoryTable)), {
      createNodeMock: () => ({ addEventListener() {}, removeEventListener() {} }),
    });
  });
  const toggle = renderer.root
    .findAllByType('button')
    .find((button) => button.props['aria-label'] === 'Toggle versions');
  expect(toggle).toBeDefined();
  act(() => toggle!.props.onClick());
}
function button(label: string) {
  function text(children: ReactNode): string {
    if (typeof children === 'string') return children;
    if (Array.isArray(children)) return children.map(text).join('');
    return '';
  }
  const found = renderer.root
    .findAllByType('button')
    .find(
      (node) => text(node.props.children) === label && typeof node.props.onClick === 'function',
    );
  expect(found).toBeDefined();
  return found!;
}
afterEach(() => {
  act(() => renderer?.unmount());
  client?.clear();
  usePlayerStore.getState().reset();
  update.mockClear();
  remove.mockReset();
  remove.mockImplementation(async () => [{ ...original, is_default: true }]);
  setDefault.mockClear();
  toast.mockClear();
  deleteMutation.isSuccess = false;
  clearMutation.isSuccess = false;
  importMutation.isSuccess = false;
  useGenerationStore.getState().removePendingGeneration('pending');
  intersect = undefined;
});
afterAll(() => {
  // Bun's module mocks survive mock.restore(). Restore this export explicitly
  // so recording-export tests in the same process use the real API client.
  mock.module('../src/lib/api/client', () => ({ apiClient: actualApiClient }));
});

describe('generation version controls', () => {
  const second = {
    ...generation,
    id: 'second',
    text: 'A different clip',
    versions: [{ ...processed, id: 'second-version', generation_id: 'second' }],
  };
  function row(text: string) {
    return renderer.root
      .findAllByProps({ role: 'button' })
      .find((node) =>
        node.findAllByType('textarea').some((textarea) => textarea.props.value === text),
      )!;
  }
  function clickRow(text: string, transcript = false, selectedText = false) {
    const node = row(text);
    const target = {
      closest: (selector: string) =>
        transcript && selector.includes('textarea')
          ? { selectionStart: 0, selectionEnd: selectedText ? 5 : 0 }
          : null,
      tagName: transcript ? 'TEXTAREA' : 'DIV',
      selectionStart: 0,
      selectionEnd: selectedText ? 5 : 0,
    };
    const originalWindow = globalThis.window;
    globalThis.window = { getSelection: () => ({ toString: () => '' }) } as unknown as Window &
      typeof globalThis;
    try {
      (node.props.onClick ?? node.props.onMouseDown)({ target, button: 0 });
    } finally {
      globalThis.window = originalWindow;
    }
  }

  test('clip actions remain outside the playback button accessibility subtree', () => {
    mount();
    const clip = row(generation.text);
    expect(clip.findAllByType('button')).toHaveLength(0);
    expect(renderer.root.findAllByProps({ 'aria-label': 'Favorite' }).length).toBeGreaterThan(0);
    expect(
      renderer.root.findAllByProps({ 'aria-label': 'Toggle versions' }).length,
    ).toBeGreaterThan(0);
  });

  test('a completed import does not erase an already-refreshed first page', () => {
    mount();
    historyData = { items: [generation, second], total: 2 };
    importMutation.isSuccess = true;
    act(() =>
      renderer.update(createElement(QueryClientProvider, { client }, createElement(HistoryTable))),
    );
    expect(row(generation.text)).toBeDefined();
    expect(row(second.text)).toBeDefined();
  });

  test('a later page refetch updates existing rows without duplicating them', () => {
    mount([generation], 2);
    laterPage = { items: [second], total: 2 };
    act(() => intersect!());
    expect(row(second.text)).toBeDefined();
    const updated = { ...second, text: 'Updated clip' };
    laterPage = { items: [updated], total: 2 };
    act(() =>
      renderer.update(createElement(QueryClientProvider, { client }, createElement(HistoryTable))),
    );
    expect(row(updated.text)).toBeDefined();
    expect(renderer.root.findAllByProps({ role: 'button' })).toHaveLength(2);
  });

  for (const [name, result] of [
    ['delete', deleteMutation],
    ['import', importMutation],
    ['clear failed', clearMutation],
  ] as const) {
    test(`${name} returns a later page to refreshed first-page results`, () => {
      mount([generation], 2);
      laterPage = { items: [second], total: 2 };
      act(() => intersect!());
      historyData = { items: [{ ...generation, text: 'Refreshed first page' }], total: 1 };
      result.isSuccess = true;
      act(() =>
        renderer.update(
          createElement(QueryClientProvider, { client }, createElement(HistoryTable)),
        ),
      );
      expect(row('Refreshed first page')).toBeDefined();
      expect(row(second.text)).toBeUndefined();
    });
  }

  test('a generation completion returns a later page to refreshed first-page results', () => {
    useGenerationStore.getState().addPendingGeneration('pending');
    mount([generation], 2);
    laterPage = { items: [second], total: 2 };
    act(() => intersect!());
    historyData = { items: [{ ...generation, text: 'Completed generation' }], total: 1 };
    act(() => useGenerationStore.getState().removePendingGeneration('pending'));
    expect(row('Completed generation')).toBeDefined();
    expect(row(second.text)).toBeUndefined();
  });

  test('switching an earlier page version updates its card after success', async () => {
    mount([generation], 2);
    laterPage = { items: [second], total: 2 };
    act(() => intersect!());
    const versionButton = renderer.root
      .findAllByType('button')
      .find((node) =>
        node.findAllByType('span').some((span) => span.props.children === 'original'),
      )!;
    await act(async () => {
      versionButton.props.onClick();
      await Promise.resolve();
    });
    expect(setDefault).toHaveBeenCalledWith('gen', 'original');
    act(() => clickRow(generation.text));
    expect(usePlayerStore.getState().audioUrl).toBe(
      '/audio/version/original?revision=original.wav',
    );
  });

  test('the final rapid card selection wins even before React rerenders', () => {
    usePlayerStore
      .getState()
      .setAudio('/audio/version/processed?revision=processed.wav', 'gen', 'voice');
    mount([generation, second]);
    act(() => {
      clickRow(second.text);
      clickRow(generation.text);
    });
    expect(usePlayerStore.getState().audioId).toBe('gen');
    expect(usePlayerStore.getState().audioUrl).toContain('/audio/version/processed');
  });

  test('seeded card gestures preserve selected clip and ignore nested controls', () => {
    const clips = [generation, second];
    const urls = [
      '/audio/version/processed?revision=processed.wav',
      '/audio/version/second-version?revision=processed.wav',
    ];
    mount(clips);
    for (const seed of [0x12345678, 0xdecafbad, 0x5eed]) {
      let random = seed >>> 0;
      const next = () => {
        random ^= random << 13;
        random ^= random >>> 17;
        random ^= random << 5;
        return random >>> 0;
      };
      act(() => usePlayerStore.getState().reset());
      let selected: number | null = null;
      const reached = [0, 0, 0, 0, 0];
      for (let step = 0; step < 40; step++) {
        const index = next() % 2;
        const action = next() % 5;
        reached[action]++;
        act(() => {
          if (action === 0) {
            clickRow(clips[index].text);
            selected = index;
          } else if (action === 1) {
            clickRow(clips[index].text, true, true);
          } else if (action === 2) {
            row(clips[index].text).props.onClick({
              button: 0,
              target: { closest: (selector: string) => (selector.includes('button') ? {} : null) },
            });
          } else if (action === 3) {
            row(clips[index].text).props.onKeyDown({
              key: ' ',
              target: { closest: () => null },
              preventDefault: () => {},
            });
            selected = index;
          } else {
            clickRow(clips[index].text);
            clickRow(clips[1 - index].text);
            clickRow(clips[index].text);
            selected = index;
          }
        });
        const player = usePlayerStore.getState();
        expect(player.audioId).toBe(selected === null ? null : clips[selected].id);
        expect(player.audioUrl).toBe(selected === null ? null : urls[selected]);
        expect(row(clips[0].text).props['aria-pressed']).toBe(selected === 0);
        expect(row(clips[1].text).props['aria-pressed']).toBe(selected === 1);
      }
      expect(reached.every((count) => count > 0)).toBe(true);
    }
  });

  test('clicking the transcript plays its card while selecting transcript text does not', () => {
    mount([generation, second]);
    act(() => clickRow(second.text, true));
    expect(usePlayerStore.getState().audioId).toBe('second');
    act(() => clickRow(generation.text, true, true));
    expect(usePlayerStore.getState().audioId).toBe('second');
  });

  test('the main card selects its current default version instead of restarting an old take', () => {
    usePlayerStore.getState().setAudio('/audio/version/original', 'gen', 'voice');
    mount();
    act(() => clickRow(generation.text));
    expect(usePlayerStore.getState().audioUrl).toBe(
      '/audio/version/processed?revision=processed.wav',
    );
    expect(usePlayerStore.getState().shouldAutoPlay).toBe(true);
    expect(row(generation.text).props['aria-pressed']).toBe(true);
  });

  test('loads existing effects and saves an empty chain to the same version', async () => {
    mount();
    act(() => button('history.versions.edit').props.onClick());
    expect(renderer.root.findByProps({ 'data-chain': true }).props.children).toContain('gain_db');
    act(() => button('Clear effects').props.onClick());
    await act(async () => {
      await button('common.save').props.onClick();
    });
    expect(update).toHaveBeenCalledWith('gen', 'processed', { effects_chain: [] });
    expect(remove).not.toHaveBeenCalled();
  });

  test('deletion needs confirmation and resets playback to the server-selected default', async () => {
    mount();
    act(() =>
      usePlayerStore.getState().setAudio('/audio/version/processed', 'gen', 'voice', 'Hello'),
    );
    act(() => button('history.versions.delete').props.onClick());
    expect(remove).not.toHaveBeenCalled();
    await act(async () => {
      await button('common.delete').props.onClick();
    });
    expect(remove).toHaveBeenCalledWith('gen', 'processed');
    expect(usePlayerStore.getState().audioUrl).toContain('/audio/version/original');
    expect(usePlayerStore.getState().shouldAutoPlay).toBe(false);
  });

  test('a delayed version deletion does not reopen audio after another selection is closed', async () => {
    let resolveDelete!: (versions: GenerationVersionResponse[]) => void;
    remove.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolveDelete = resolve;
        }),
    );
    mount([generation, second]);
    act(() => usePlayerStore.getState().setAudio('/audio/version/processed', 'gen', 'voice'));
    act(() => button('history.versions.delete').props.onClick());
    let pending!: Promise<void>;
    await act(async () => {
      pending = button('common.delete').props.onClick();
      await Promise.resolve();
    });
    act(() => {
      usePlayerStore.getState().setAudio('/audio/version/second-version', 'second', 'voice');
      usePlayerStore.getState().reset();
    });
    await act(async () => {
      resolveDelete([{ ...original, is_default: true }]);
      await pending;
    });
    expect(usePlayerStore.getState().audioId).toBeNull();
    expect(usePlayerStore.getState().audioUrl).toBeNull();
  });

  test('a failed edit keeps the editor open for correction', async () => {
    update.mockRejectedValueOnce(new Error('Source audio file not found'));
    mount();
    act(() => button('history.versions.edit').props.onClick());
    await act(async () => {
      await button('common.save').props.onClick();
    });
    expect(button('common.save').props.disabled).toBe(false);
    expect(toast).toHaveBeenCalledWith(
      expect.objectContaining({
        description: 'Source audio file not found',
        variant: 'destructive',
      }),
    );
  });

  test('a failed deletion preserves the confirmation and restores selected audio', async () => {
    remove.mockRejectedValueOnce(new Error('This version is pinned in a story'));
    mount();
    act(() =>
      usePlayerStore.getState().setAudio('/audio/version/processed', 'gen', 'voice', 'Hello'),
    );
    act(() => button('history.versions.delete').props.onClick());
    await act(async () => {
      await button('common.delete').props.onClick();
    });
    expect(button('common.delete').props.disabled).toBe(false);
    expect(usePlayerStore.getState().audioUrl).toBe('/audio/version/processed');
    expect(toast).toHaveBeenCalledWith(
      expect.objectContaining({
        description: 'This version is pinned in a story',
        variant: 'destructive',
      }),
    );
  });
});
