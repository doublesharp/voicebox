import { afterEach, describe, expect, mock, test } from 'bun:test';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { createElement, Fragment, type ReactNode } from 'react';
import { act, create, type ReactTestRenderer } from 'react-test-renderer';
import type { GenerationVersionResponse, HistoryResponse } from '../src/lib/api/types';
import { usePlayerStore } from '../src/stores/playerStore';

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
const historyData = { items: [generation], total: 1 };
const mutation = { mutate: mock(() => {}), isPending: false, isSuccess: false };
const update = mock(
  async (_generation: string, _id: string, data: { effects_chain: unknown[] }) => ({
    ...processed,
    audio_path: 'updated.wav',
    effects_chain: data.effects_chain,
  }),
);
const remove = mock(async () => [{ ...original, is_default: true }]);
const toast = mock(() => {});

mock.module('../src/lib/hooks/useHistory', () => ({
  useHistory: () => ({ data: historyData, isLoading: false, isFetching: false }),
  useDeleteGeneration: () => mutation,
  useClearFailedGenerations: () => mutation,
  useExportGeneration: () => mutation,
  useExportGenerationAudio: () => mutation,
  useImportGeneration: () => mutation,
}));
mock.module('../src/lib/api/client', () => ({
  apiClient: {
    updateGenerationVersion: update,
    deleteGenerationVersion: remove,
    getVersionAudioUrl: (id: string, revision: string) =>
      `/audio/version/${id}?revision=${revision}`,
    setDefaultVersion: mock(async () => {}),
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
    observe() {}
    disconnect() {}
  },
});

const { HistoryTable } = await import('../src/components/History/HistoryTable');
let renderer: ReactTestRenderer;
let client: QueryClient;
function mount() {
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  act(() => {
    renderer = create(createElement(QueryClientProvider, { client }, createElement(HistoryTable)));
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
  remove.mockClear();
  toast.mockClear();
});

describe('generation version controls', () => {
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
