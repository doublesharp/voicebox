import { useMutation, useQueryClient } from '@tanstack/react-query';
import { AnimatePresence, motion } from 'framer-motion';
import {
  AudioLines,
  Download,
  FileArchive,
  Loader2,
  MoreHorizontal,
  Pencil,
  Play,
  RotateCcw,
  Square,
  Star,
  Trash2,
  Wand2,
} from 'lucide-react';
import { useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';

import { AudioBars } from '@/components/AudioBars';
import { EffectsChainEditor } from '@/components/Effects/EffectsChainEditor';
import { Button } from '@/components/ui/button';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { Textarea } from '@/components/ui/textarea';
import { useToast } from '@/components/ui/use-toast';
import { apiClient } from '@/lib/api/client';
import type {
  EffectConfig,
  GenerationExportFormat,
  GenerationVersionResponse,
  HistoryResponse,
} from '@/lib/api/types';
import { BOTTOM_SAFE_AREA_PADDING } from '@/lib/constants/ui';
import {
  useClearFailedGenerations,
  useDeleteGeneration,
  useExportGeneration,
  useExportGenerationAudio,
  useHistory,
  useImportGeneration,
} from '@/lib/hooks/useHistory';
import { cn } from '@/lib/utils/cn';
import { formatDate, formatDuration, formatEngineName } from '@/lib/utils/format';
import { useGenerationStore } from '@/stores/generationStore';
import { usePlayerStore } from '@/stores/playerStore';

export function HistoryTable() {
  const { t } = useTranslation();
  const [page, setPage] = useState(0);
  const [allHistory, setAllHistory] = useState<HistoryResponse[]>([]);
  const [total, setTotal] = useState(0);
  const [isScrolled, setIsScrolled] = useState(false);
  const scrollRef = useRef<HTMLDivElement>(null);
  const loadMoreRef = useRef<HTMLDivElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [importDialogOpen, setImportDialogOpen] = useState(false);
  const [selectedFile, setSelectedFile] = useState<File | null>(null);
  const [deleteDialogOpen, setDeleteDialogOpen] = useState(false);
  const [generationToDelete, setGenerationToDelete] = useState<{ id: string; name: string } | null>(
    null,
  );
  const [effectsDialogOpen, setEffectsDialogOpen] = useState(false);
  const [effectsTargetId, setEffectsTargetId] = useState<string | null>(null);
  const [effectsTargetVersions, setEffectsTargetVersions] = useState<GenerationVersionResponse[]>(
    [],
  );
  const [effectsSourceVersionId, setEffectsSourceVersionId] = useState<string | null>(null);
  const [effectsChain, setEffectsChain] = useState<EffectConfig[]>([]);
  const [applyingEffects, setApplyingEffects] = useState(false);
  const [editingVersion, setEditingVersion] = useState<GenerationVersionResponse | null>(null);
  const [versionToDelete, setVersionToDelete] = useState<GenerationVersionResponse | null>(null);
  const [deletingVersion, setDeletingVersion] = useState(false);
  const versionMutationRef = useRef(false);
  const [expandedVersionsId, setExpandedVersionsId] = useState<string | null>(null);
  const limit = 20;
  const { toast } = useToast();
  const queryClient = useQueryClient();

  const {
    data: historyData,
    isLoading,
    isFetching,
  } = useHistory({
    limit,
    offset: page * limit,
  });

  const deleteGeneration = useDeleteGeneration();
  const clearFailed = useClearFailedGenerations();
  const [clearFailedDialogOpen, setClearFailedDialogOpen] = useState(false);
  const exportGeneration = useExportGeneration();
  const exportGenerationAudio = useExportGenerationAudio();
  const importGeneration = useImportGeneration();
  const cancelGeneration = useMutation({
    mutationFn: (generationId: string) => apiClient.cancelGeneration(generationId),
    onSuccess: async (data) => {
      await queryClient.invalidateQueries({ queryKey: ['history'] });
      toast({
        title: 'Cancelling generation',
        description: data.message,
      });
    },
    onError: (error) => {
      toast({
        title: 'Cancel failed',
        description: error instanceof Error ? error.message : 'Could not cancel generation',
        variant: 'destructive',
      });
    },
  });
  const addPendingGeneration = useGenerationStore((state) => state.addPendingGeneration);
  const setAudioWithAutoPlay = usePlayerStore((state) => state.setAudioWithAutoPlay);
  const restartCurrentAudio = usePlayerStore((state) => state.restartCurrentAudio);
  const currentAudioId = usePlayerStore((state) => state.audioId);
  const isPlaying = usePlayerStore((state) => state.isPlaying);
  const audioUrl = usePlayerStore((state) => state.audioUrl);
  const isPlayerVisible = !!audioUrl;

  // Update accumulated history when new data arrives
  useEffect(() => {
    if (historyData?.items) {
      setTotal(historyData.total);
      if (page === 0) {
        // Reset to first page
        setAllHistory(historyData.items);
      } else {
        // Append new items, avoiding duplicates
        setAllHistory((prev) => {
          const existingIds = new Set(prev.map((item) => item.id));
          const newItems = historyData.items.filter((item) => !existingIds.has(item.id));
          return [...prev, ...newItems];
        });
      }
    }
  }, [historyData, page]);

  // Reset to page 0 when deletions, imports, or generation completions occur
  const pendingCount = useGenerationStore((state) => state.pendingGenerationIds.size);
  const prevPendingCountRef = useRef(pendingCount);
  useEffect(() => {
    if (deleteGeneration.isSuccess || importGeneration.isSuccess || clearFailed.isSuccess) {
      setPage(0);
      setAllHistory([]);
    }
  }, [deleteGeneration.isSuccess, importGeneration.isSuccess, clearFailed.isSuccess]);

  useEffect(() => {
    // A generation finished (pending count decreased) — scroll back to show it
    if (
      prevPendingCountRef.current > 0 &&
      pendingCount < prevPendingCountRef.current &&
      page !== 0
    ) {
      setPage(0);
      setAllHistory([]);
    }
    prevPendingCountRef.current = pendingCount;
  }, [pendingCount, page]);

  // Intersection Observer for infinite scroll
  useEffect(() => {
    const loadMoreEl = loadMoreRef.current;
    if (!loadMoreEl) return;

    const observer = new IntersectionObserver(
      (entries) => {
        const target = entries[0];
        if (target.isIntersecting && !isFetching && allHistory.length < total) {
          setPage((prev) => prev + 1);
        }
      },
      {
        root: scrollRef.current,
        rootMargin: '100px',
        threshold: 0.1,
      },
    );

    observer.observe(loadMoreEl);
    return () => observer.disconnect();
  }, [isFetching, allHistory.length, total]);

  // Track scroll position for gradient effect
  useEffect(() => {
    const scrollEl = scrollRef.current;
    if (!scrollEl) return;

    const handleScroll = () => {
      setIsScrolled(scrollEl.scrollTop > 0);
    };

    scrollEl.addEventListener('scroll', handleScroll);
    return () => scrollEl.removeEventListener('scroll', handleScroll);
  }, []);

  const handlePlay = (audioId: string, text: string, profileId: string) => {
    // If clicking the same audio, restart it from the beginning
    if (currentAudioId === audioId) {
      restartCurrentAudio();
    } else {
      // Otherwise, load the new audio and auto-play it
      const audioUrl = apiClient.getAudioUrl(audioId);
      setAudioWithAutoPlay(audioUrl, audioId, profileId, text.substring(0, 50));
    }
  };

  const handleDownloadAudio = (
    generationId: string,
    text: string,
    format: GenerationExportFormat = 'wav',
  ) => {
    exportGenerationAudio.mutate(
      { generationId, text, format },
      {
        onError: (error) => {
          toast({
            title: 'Failed to download audio',
            description: error.message,
            variant: 'destructive',
          });
        },
      },
    );
  };

  const handleExportPackage = (generationId: string, text: string) => {
    exportGeneration.mutate(
      { generationId, text },
      {
        onError: (error) => {
          toast({
            title: 'Failed to export generation',
            description: error.message,
            variant: 'destructive',
          });
        },
      },
    );
  };

  const handleDeleteClick = (generationId: string, profileName: string) => {
    setGenerationToDelete({ id: generationId, name: profileName });
    setDeleteDialogOpen(true);
  };

  const handleDeleteConfirm = () => {
    if (generationToDelete) {
      deleteGeneration.mutate(generationToDelete.id);
      setDeleteDialogOpen(false);
      setGenerationToDelete(null);
    }
  };

  const handleRetry = async (generationId: string) => {
    try {
      const result = await apiClient.retryGeneration(generationId);
      addPendingGeneration(result.id);
      queryClient.invalidateQueries({ queryKey: ['history'] });
    } catch (error) {
      toast({
        title: 'Retry failed',
        description: error instanceof Error ? error.message : 'Could not retry generation',
        variant: 'destructive',
      });
    }
  };

  const handleRegenerate = async (generationId: string) => {
    try {
      await apiClient.regenerateGeneration(generationId);
      addPendingGeneration(generationId);
      queryClient.invalidateQueries({ queryKey: ['history'] });
    } catch (error) {
      toast({
        title: 'Regenerate failed',
        description: error instanceof Error ? error.message : 'Could not regenerate',
        variant: 'destructive',
      });
    }
  };

  const handleToggleFavorite = async (generationId: string) => {
    try {
      await apiClient.toggleFavorite(generationId);
      queryClient.invalidateQueries({ queryKey: ['history'] });
    } catch (error) {
      toast({
        title: 'Failed to update favorite',
        description: error instanceof Error ? error.message : 'Unknown error',
        variant: 'destructive',
      });
    }
  };

  const handleApplyEffects = (generationId: string) => {
    const gen = allHistory.find((g) => g.id === generationId);
    const versions = gen?.versions ?? [];
    setEffectsTargetId(generationId);
    setEffectsTargetVersions(versions);
    // Default to clean/original version (no effects chain)
    const cleanVersion = versions.find((v) => !v.effects_chain || v.effects_chain.length === 0);
    setEffectsSourceVersionId(cleanVersion?.id ?? null);
    setEffectsChain([]);
    setEditingVersion(null);
    setEffectsDialogOpen(true);
  };

  const handleEditVersion = (generationId: string, version: GenerationVersionResponse) => {
    setEffectsTargetId(generationId);
    setEditingVersion(version);
    setEffectsChain(structuredClone(version.effects_chain ?? []));
    setEffectsDialogOpen(true);
  };

  const updateVersions = (generationId: string, versions: GenerationVersionResponse[]) => {
    const active = versions.find((version) => version.is_default);
    // Update accumulated pages too: invalidating only the current page leaves
    // versions on earlier pages stale until History is reopened.
    setAllHistory((previous) =>
      previous.map((generation) =>
        generation.id === generationId
          ? { ...generation, versions, audio_path: active?.audio_path ?? generation.audio_path }
          : generation,
      ),
    );
    queryClient.invalidateQueries({ queryKey: ['history'] });
    queryClient.invalidateQueries({ queryKey: ['stories'] });
  };

  const handleDeleteVersionConfirm = async () => {
    if (!versionToDelete || versionMutationRef.current) return;
    versionMutationRef.current = true;
    setDeletingVersion(true);
    const target = versionToDelete;
    const player = usePlayerStore.getState();
    const wasSelected = player.audioId === target.generation_id;
    // Release the media file before deletion, particularly on Windows.
    if (wasSelected) player.reset();
    try {
      const versions = await apiClient.deleteGenerationVersion(target.generation_id, target.id);
      updateVersions(target.generation_id, versions);
      const active = versions.find((version) => version.is_default);
      if (wasSelected && active && !usePlayerStore.getState().audioId) {
        usePlayerStore
          .getState()
          .setAudio(
            apiClient.getVersionAudioUrl(active.id, active.audio_path),
            target.generation_id,
            player.profileId,
            player.title ?? undefined,
          );
      }
      setVersionToDelete(null);
      toast({ title: t('history.versions.deleted') });
    } catch (error) {
      if (wasSelected && player.audioUrl && !usePlayerStore.getState().audioId) {
        usePlayerStore
          .getState()
          .setAudio(
            player.audioUrl,
            target.generation_id,
            player.profileId,
            player.title ?? undefined,
          );
      }
      toast({
        title: t('history.versions.deleteFailed'),
        description: error instanceof Error ? error.message : t('common.unknownError'),
        variant: 'destructive',
      });
    } finally {
      versionMutationRef.current = false;
      setDeletingVersion(false);
    }
  };

  const handleApplyEffectsConfirm = async () => {
    if (
      !effectsTargetId ||
      (!editingVersion && effectsChain.length === 0) ||
      versionMutationRef.current
    )
      return;
    versionMutationRef.current = true;
    setApplyingEffects(true);
    try {
      const newVersion = editingVersion
        ? await apiClient.updateGenerationVersion(effectsTargetId, editingVersion.id, {
            effects_chain: effectsChain,
          })
        : await apiClient.applyEffectsToGeneration(effectsTargetId, {
            effects_chain: effectsChain,
            source_version_id: effectsSourceVersionId ?? undefined,
            set_as_default: true,
          });
      const generation = allHistory.find((item) => item.id === effectsTargetId);
      const versions = generation?.versions ?? [];
      updateVersions(
        effectsTargetId,
        editingVersion
          ? versions.map((version) => (version.id === newVersion.id ? newVersion : version))
          : [...versions.map((version) => ({ ...version, is_default: false })), newVersion],
      );

      // If the player is currently on this generation, reload with the new version audio
      const player = usePlayerStore.getState();
      const isEditedAudio = player.audioUrl?.includes(`/audio/version/${editingVersion?.id}`);
      if (
        player.audioId === effectsTargetId &&
        (!editingVersion || newVersion.is_default || isEditedAudio)
      ) {
        const gen = allHistory.find((g) => g.id === effectsTargetId);
        if (gen) {
          const versionUrl = apiClient.getVersionAudioUrl(newVersion.id, newVersion.audio_path);
          const setAudio = player.isPlaying ? player.setAudioWithAutoPlay : player.setAudio;
          setAudio(versionUrl, effectsTargetId, gen.profile_id, gen.text.substring(0, 50));
        }
      }

      setEffectsDialogOpen(false);
      toast({
        title: editingVersion ? t('history.versions.updated') : 'Effects applied',
        description: editingVersion ? undefined : 'A new version has been created.',
      });
    } catch (error) {
      toast({
        title: editingVersion ? t('history.versions.updateFailed') : 'Failed to apply effects',
        description: error instanceof Error ? error.message : 'Unknown error',
        variant: 'destructive',
      });
    } finally {
      versionMutationRef.current = false;
      setApplyingEffects(false);
    }
  };

  const handleSwitchVersion = async (generationId: string, versionId: string) => {
    try {
      await apiClient.setDefaultVersion(generationId, versionId);
      queryClient.invalidateQueries({ queryKey: ['history'] });
    } catch (error) {
      toast({
        title: 'Failed to switch version',
        description: error instanceof Error ? error.message : 'Unknown error',
        variant: 'destructive',
      });
    }
  };

  const handlePlayVersion = (
    generationId: string,
    versionId: string,
    text: string,
    profileId: string,
  ) => {
    const version = allHistory
      .find((gen) => gen.id === generationId)
      ?.versions?.find((v) => v.id === versionId);
    const audioUrl = apiClient.getVersionAudioUrl(versionId, version?.audio_path);
    setAudioWithAutoPlay(audioUrl, generationId, profileId, text.substring(0, 50));
  };

  const handleImportConfirm = () => {
    if (selectedFile) {
      importGeneration.mutate(selectedFile, {
        onSuccess: (data) => {
          setImportDialogOpen(false);
          setSelectedFile(null);
          if (fileInputRef.current) {
            fileInputRef.current.value = '';
          }
          toast({
            title: 'Generation imported',
            description: data.message || 'Generation imported successfully',
          });
        },
        onError: (error) => {
          toast({
            title: 'Failed to import generation',
            description: error.message,
            variant: 'destructive',
          });
        },
      });
    }
  };

  if (isLoading && page === 0) {
    return (
      <div className="flex items-center justify-center h-full">
        <Loader2 className="h-8 w-8 animate-spin text-muted-foreground" />
      </div>
    );
  }

  const history = allHistory;
  const hasMore = allHistory.length < total;
  const failedCount = history.filter((g) => g.status === 'failed').length;

  const handleClearFailedConfirm = () => {
    clearFailed.mutate(undefined, {
      onSuccess: (data) => {
        setClearFailedDialogOpen(false);
        toast({
          title: 'Cleared failed generations',
          description: `${data.deleted} failed ${data.deleted === 1 ? 'generation' : 'generations'} removed.`,
        });
      },
      onError: (error) => {
        setClearFailedDialogOpen(false);
        toast({
          title: 'Failed to clear',
          description: error instanceof Error ? error.message : 'Unknown error',
          variant: 'destructive',
        });
      },
    });
  };

  return (
    <div className="flex flex-col h-full min-h-0 relative">
      {history.length === 0 ? (
        <div className="text-center py-12 pt-4 lg:pt-12 px-5 border-2 border-dashed mb-5 border-muted rounded-md text-muted-foreground flex-1 flex items-start lg:items-center justify-center">
          {t('history.empty')}
        </div>
      ) : (
        <>
          {failedCount > 0 && (
            <div className="flex items-center justify-between px-1 pb-2">
              <span className="text-xs text-muted-foreground">
                {failedCount} failed {failedCount === 1 ? 'generation' : 'generations'}
              </span>
              <Button
                variant="ghost"
                size="sm"
                className="h-7 text-xs text-muted-foreground"
                onClick={() => setClearFailedDialogOpen(true)}
                disabled={clearFailed.isPending}
              >
                <Trash2 className="h-3 w-3 mr-1.5" />
                {clearFailed.isPending ? 'Clearing...' : 'Clear failed'}
              </Button>
            </div>
          )}
          {isScrolled && (
            <div className="absolute top-0 left-0 right-0 h-16 bg-gradient-to-b from-background to-transparent z-10 pointer-events-none" />
          )}
          <div
            ref={scrollRef}
            className={cn(
              'flex-1 min-h-0 overflow-y-auto space-y-2 pb-4',
              isPlayerVisible && BOTTOM_SAFE_AREA_PADDING,
            )}
          >
            {history.map((gen) => {
              const isCurrentlyPlaying = currentAudioId === gen.id && isPlaying;
              const isInProgress = gen.status === 'loading_model' || gen.status === 'generating';
              const isGenerating = isInProgress;
              const isFailed = gen.status === 'failed';
              const isPlayable = !isGenerating && !isFailed;
              const hasVersions = gen.versions && gen.versions.length > 1;
              const isVersionsExpanded = expandedVersionsId === gen.id;
              const isCancelling =
                cancelGeneration.isPending && cancelGeneration.variables === gen.id;
              return (
                <div
                  key={gen.id}
                  className={cn(
                    'border rounded-md bg-card transition-colors text-left w-full',
                    isCurrentlyPlaying && 'bg-muted/70',
                  )}
                >
                  {/* Main row */}
                  {/* biome-ignore lint/a11y/useSemanticElements: The row contains a transcript and action buttons, which cannot be nested inside a native button. */}
                  <div
                    role="button"
                    aria-disabled={!isPlayable}
                    tabIndex={isPlayable ? 0 : undefined}
                    className={cn(
                      'flex items-stretch gap-4 h-26 p-3 outline-none',
                      isPlayable && 'hover:bg-muted/70 cursor-pointer rounded-md',
                      isVersionsExpanded && 'rounded-b-none',
                    )}
                    aria-label={
                      isGenerating
                        ? `Generating speech for ${gen.profile_name}...`
                        : isFailed
                          ? `Generation failed for ${gen.profile_name}`
                          : isCurrentlyPlaying
                            ? `Sample from ${gen.profile_name}, ${formatDuration(gen.duration ?? 0)}, ${formatDate(gen.created_at)}. Playing. Press Enter to restart.`
                            : `Sample from ${gen.profile_name}, ${formatDuration(gen.duration ?? 0)}, ${formatDate(gen.created_at)}. Press Enter to play.`
                    }
                    onMouseDown={(e) => {
                      if (!isPlayable) return;
                      const target = e.target as HTMLElement;
                      if (
                        target.closest('textarea, button, [role="menuitem"]') ||
                        window.getSelection()?.toString()
                      ) {
                        return;
                      }
                      handlePlay(gen.id, gen.text, gen.profile_id);
                    }}
                    onKeyDown={(e) => {
                      if (!isPlayable) return;
                      const target = e.target as HTMLElement;
                      if (target.closest('textarea') || target.closest('button')) return;
                      if (e.key === 'Enter' || e.key === ' ') {
                        e.preventDefault();
                        handlePlay(gen.id, gen.text, gen.profile_id);
                      }
                    }}
                  >
                    {/* Status icon */}
                    <div className="flex items-center shrink-0 w-10 justify-center overflow-hidden">
                      <AudioBars
                        mode={isGenerating ? 'generating' : isCurrentlyPlaying ? 'playing' : 'idle'}
                      />
                    </div>

                    {/* Left side - Meta information */}
                    <div className="flex flex-col gap-1.5 w-48 shrink-0 justify-center">
                      <div className="font-medium text-sm truncate" title={gen.profile_name}>
                        {gen.profile_name}
                      </div>
                      <div className="flex items-center gap-2">
                        <span className="text-xs text-muted-foreground">{gen.language}</span>
                        <span className="text-xs text-muted-foreground">
                          {formatEngineName(gen.engine, gen.model_size)}
                        </span>
                        {isFailed ? (
                          <span className="text-xs text-destructive">Failed</span>
                        ) : !isGenerating ? (
                          <span className="text-xs text-muted-foreground">
                            {formatDuration(gen.duration ?? 0)}
                          </span>
                        ) : null}
                      </div>
                      <div className="text-xs text-muted-foreground">
                        {isInProgress ? (
                          <span className="text-accent">
                            {gen.status === 'loading_model' ? 'Loading model...' : 'Generating...'}
                          </span>
                        ) : (
                          formatDate(gen.created_at)
                        )}
                      </div>
                    </div>

                    {/* Right side - Transcript textarea */}
                    <div className="flex-1 min-w-0 flex">
                      <Textarea
                        value={gen.text}
                        className="flex-1 resize-none text-sm text-muted-foreground select-text"
                        readOnly
                        aria-label={`Transcript for sample from ${gen.profile_name}, ${formatDuration(gen.duration ?? 0)}`}
                      />
                    </div>

                    {/* Far right - Actions */}
                    <div className="shrink-0 flex flex-col justify-center items-center gap-0.5">
                      <Button
                        variant="ghost"
                        size="icon"
                        className={cn(
                          'h-6 w-6 text-muted-foreground/50 hover:bg-muted-foreground/20 hover:text-muted-foreground',
                          gen.is_favorited && 'text-accent hover:text-accent',
                        )}
                        aria-label={gen.is_favorited ? 'Unfavorite' : 'Favorite'}
                        onClick={() => handleToggleFavorite(gen.id)}
                      >
                        <Star
                          className="h-2 w-2"
                          fill={gen.is_favorited ? 'currentColor' : 'none'}
                        />
                      </Button>
                      {hasVersions && (
                        <Button
                          variant="ghost"
                          size="icon"
                          className={cn(
                            'h-6 w-6 text-muted-foreground/50 hover:bg-muted-foreground/20 hover:text-muted-foreground',
                            isVersionsExpanded && 'text-accent hover:text-accent',
                          )}
                          aria-label="Toggle versions"
                          onClick={() => setExpandedVersionsId(isVersionsExpanded ? null : gen.id)}
                        >
                          <AudioLines className="h-2 w-2" />
                        </Button>
                      )}

                      {isFailed ? (
                        <>
                          <Button
                            variant="ghost"
                            size="icon"
                            className="h-6 w-6 text-muted-foreground/50 hover:bg-muted-foreground/20 hover:text-muted-foreground"
                            aria-label="Retry generation"
                            onClick={() => handleRetry(gen.id)}
                          >
                            <RotateCcw className="h-2 w-2" />
                          </Button>
                          <Button
                            variant="ghost"
                            size="icon"
                            className="h-6 w-6 text-muted-foreground/50 hover:bg-muted-foreground/20 hover:text-muted-foreground"
                            aria-label="Delete generation"
                            disabled={deleteGeneration.isPending}
                            onClick={() => handleDeleteClick(gen.id, gen.profile_name)}
                          >
                            <Trash2 className="h-2 w-2" />
                          </Button>
                        </>
                      ) : isGenerating ? (
                        <Button
                          variant="ghost"
                          size="icon"
                          className="h-6 w-6 text-muted-foreground/50 hover:bg-muted-foreground/20 hover:text-muted-foreground"
                          aria-label="Cancel generation"
                          disabled={isCancelling}
                          onClick={() => cancelGeneration.mutate(gen.id)}
                        >
                          {isCancelling ? (
                            <Loader2 className="h-2 w-2 animate-spin" />
                          ) : (
                            <Square className="h-2 w-2" />
                          )}
                        </Button>
                      ) : (
                        <DropdownMenu>
                          <DropdownMenuTrigger asChild>
                            <Button
                              variant="ghost"
                              size="icon"
                              className="h-6 w-6 text-muted-foreground/50 hover:bg-muted-foreground/20 hover:text-muted-foreground"
                              aria-label={t('history.actions.menu')}
                              disabled={isGenerating}
                            >
                              <MoreHorizontal className="h-2 w-2" />
                            </Button>
                          </DropdownMenuTrigger>
                          <DropdownMenuContent align="end">
                            <DropdownMenuItem
                              onClick={() => handlePlay(gen.id, gen.text, gen.profile_id)}
                            >
                              <Play className="mr-2 h-4 w-4" />
                              {t('history.actions.play')}
                            </DropdownMenuItem>
                            <DropdownMenuItem
                              onClick={() => handleDownloadAudio(gen.id, gen.text)}
                              disabled={exportGenerationAudio.isPending}
                            >
                              <Download className="mr-2 h-4 w-4" />
                              {t('history.actions.exportAudio')}
                            </DropdownMenuItem>
                            <DropdownMenuItem
                              onClick={() => handleDownloadAudio(gen.id, gen.text, 'mp3')}
                              disabled={exportGenerationAudio.isPending}
                            >
                              <Download className="mr-2 h-4 w-4" />
                              {t('history.actions.exportAudioMp3')}
                            </DropdownMenuItem>
                            <DropdownMenuItem
                              onClick={() => handleExportPackage(gen.id, gen.text)}
                              disabled={exportGeneration.isPending}
                            >
                              <FileArchive className="mr-2 h-4 w-4" />
                              {t('history.actions.exportPackage')}
                            </DropdownMenuItem>
                            <DropdownMenuItem onClick={() => handleApplyEffects(gen.id)}>
                              <Wand2 className="mr-2 h-4 w-4" />
                              {t('history.actions.applyEffects')}
                            </DropdownMenuItem>
                            <DropdownMenuItem onClick={() => handleRegenerate(gen.id)}>
                              <RotateCcw className="mr-2 h-4 w-4" />
                              {t('history.actions.regenerate')}
                            </DropdownMenuItem>
                            <DropdownMenuItem
                              onClick={() => handleDeleteClick(gen.id, gen.profile_name)}
                              disabled={deleteGeneration.isPending}
                            >
                              <Trash2 className="mr-2 h-4 w-4" />
                              {t('common.delete')}
                            </DropdownMenuItem>
                          </DropdownMenuContent>
                        </DropdownMenu>
                      )}
                    </div>
                  </div>

                  {/* Expandable versions panel */}
                  <AnimatePresence>
                    {isVersionsExpanded && gen.versions && (
                      <motion.div
                        initial={{ height: 0, opacity: 0 }}
                        animate={{ height: 'auto', opacity: 1 }}
                        exit={{ height: 0, opacity: 0 }}
                        transition={{ duration: 0.2, ease: 'easeOut' }}
                        className="overflow-hidden"
                      >
                        <div className="border-t border-border/50">
                          <div className="divide-y divide-border/40">
                            {gen.versions.map((v) => {
                              // Show source provenance when effects were applied to a non-clean version
                              const sourceVersion = v.source_version_id
                                ? gen.versions?.find((sv) => sv.id === v.source_version_id)
                                : null;
                              const showSource = (sourceVersion?.effects_chain?.length ?? 0) > 0;
                              const original = gen.versions?.find(
                                (version) => !version.effects_chain && !version.source_version_id,
                              );
                              const isOriginal = v.id === original?.id;
                              const hasDerivedVersions = gen.versions?.some(
                                (version) => version.source_version_id === v.id,
                              );
                              const canEdit =
                                !isOriginal && (!!v.effects_chain || !!v.source_version_id);

                              return (
                                <div
                                  key={v.id}
                                  className="flex items-center pr-2 hover:bg-muted/50 transition-colors"
                                >
                                  <button
                                    type="button"
                                    className="flex items-center gap-2 flex-1 min-w-0 h-9 px-3 text-left"
                                    disabled={applyingEffects || deletingVersion}
                                    onClick={() => {
                                      handlePlayVersion(gen.id, v.id, gen.text, gen.profile_id);
                                      if (!v.is_default) {
                                        handleSwitchVersion(gen.id, v.id);
                                      }
                                    }}
                                  >
                                    <AudioLines className="h-3 w-3 shrink-0 text-muted-foreground" />
                                    <span className="truncate text-xs font-medium">{v.label}</span>
                                    {v.effects_chain && v.effects_chain.length > 0 && (
                                      <span className="text-[10px] text-muted-foreground truncate">
                                        {v.effects_chain.map((e) => e.type).join(' → ')}
                                      </span>
                                    )}
                                    {showSource && (
                                      <span className="text-[10px] text-muted-foreground/60 truncate">
                                        from {sourceVersion?.label}
                                      </span>
                                    )}
                                    <span className="flex-1" />
                                    {v.is_default && (
                                      <span className="text-[10px] bg-accent/15 text-accent px-1.5 py-0.5 rounded-full">
                                        active
                                      </span>
                                    )}
                                  </button>
                                  {!isOriginal && (
                                    <DropdownMenu>
                                      <DropdownMenuTrigger asChild>
                                        <Button
                                          variant="ghost"
                                          size="icon"
                                          className="h-7 w-7 shrink-0 text-muted-foreground"
                                          aria-label={t('history.versions.actions', {
                                            name: v.label,
                                          })}
                                          disabled={applyingEffects || deletingVersion}
                                        >
                                          <MoreHorizontal className="h-3.5 w-3.5" />
                                        </Button>
                                      </DropdownMenuTrigger>
                                      <DropdownMenuContent align="end">
                                        {canEdit && (
                                          <DropdownMenuItem
                                            disabled={hasDerivedVersions}
                                            onSelect={() => handleEditVersion(gen.id, v)}
                                          >
                                            <Pencil className="mr-2 h-4 w-4" />
                                            {t('history.versions.edit')}
                                          </DropdownMenuItem>
                                        )}
                                        <DropdownMenuItem
                                          className="text-destructive focus:text-destructive"
                                          disabled={
                                            hasDerivedVersions || (gen.versions?.length ?? 0) <= 1
                                          }
                                          onSelect={() => setVersionToDelete(v)}
                                        >
                                          <Trash2 className="mr-2 h-4 w-4" />
                                          {t('history.versions.delete')}
                                        </DropdownMenuItem>
                                        {hasDerivedVersions && (
                                          <p className="max-w-56 px-2 py-1.5 text-xs text-muted-foreground">
                                            {t('history.versions.usedAsSource')}
                                          </p>
                                        )}
                                      </DropdownMenuContent>
                                    </DropdownMenu>
                                  )}
                                </div>
                              );
                            })}
                          </div>
                        </div>
                      </motion.div>
                    )}
                  </AnimatePresence>
                </div>
              );
            })}

            {/* Load more trigger element */}
            {hasMore && (
              <div ref={loadMoreRef} className="flex items-center justify-center py-4">
                {isFetching && <Loader2 className="h-6 w-6 animate-spin text-muted-foreground" />}
              </div>
            )}

            {/* End of list indicator */}
            {!hasMore && history.length > 0 && (
              <div className="text-center py-4 text-xs text-muted-foreground">
                You've reached the end
              </div>
            )}
          </div>
        </>
      )}

      <Dialog open={deleteDialogOpen} onOpenChange={setDeleteDialogOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{t('history.deleteDialog.title')}</DialogTitle>
            <DialogDescription>
              {t('history.deleteDialog.body', { name: generationToDelete?.name })}
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button
              variant="outline"
              onClick={() => {
                setDeleteDialogOpen(false);
                setGenerationToDelete(null);
              }}
            >
              {t('common.cancel')}
            </Button>
            <Button
              variant="destructive"
              onClick={handleDeleteConfirm}
              disabled={deleteGeneration.isPending}
            >
              {deleteGeneration.isPending ? t('history.deleteDialog.deleting') : t('common.delete')}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog open={clearFailedDialogOpen} onOpenChange={setClearFailedDialogOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{t('history.clearFailedDialog.title')}</DialogTitle>
            <DialogDescription>
              {t('history.clearFailedDialog.body', { count: failedCount })}
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => setClearFailedDialogOpen(false)}>
              {t('common.cancel')}
            </Button>
            <Button
              variant="destructive"
              onClick={handleClearFailedConfirm}
              disabled={clearFailed.isPending}
            >
              {clearFailed.isPending
                ? t('history.clearFailedDialog.clearing')
                : t('history.clearFailedDialog.clearAll')}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog open={importDialogOpen} onOpenChange={setImportDialogOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{t('history.importDialog.title')}</DialogTitle>
            <DialogDescription>
              {t('history.importDialog.body', { name: selectedFile?.name })}
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button
              variant="outline"
              onClick={() => {
                setImportDialogOpen(false);
                setSelectedFile(null);
                if (fileInputRef.current) {
                  fileInputRef.current.value = '';
                }
              }}
            >
              {t('common.cancel')}
            </Button>
            <Button
              onClick={handleImportConfirm}
              disabled={importGeneration.isPending || !selectedFile}
            >
              {importGeneration.isPending
                ? t('history.importDialog.importing')
                : t('history.importDialog.action')}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog
        open={!!versionToDelete}
        onOpenChange={(open) => {
          if (!open && !deletingVersion) setVersionToDelete(null);
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{t('history.versions.delete')}</DialogTitle>
            <DialogDescription>
              {t('history.versions.deleteBody', { name: versionToDelete?.label })}
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button
              variant="outline"
              disabled={deletingVersion}
              onClick={() => setVersionToDelete(null)}
            >
              {t('common.cancel')}
            </Button>
            <Button
              variant="destructive"
              disabled={deletingVersion}
              onClick={handleDeleteVersionConfirm}
            >
              {deletingVersion ? t('history.deleteDialog.deleting') : t('common.delete')}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog
        open={effectsDialogOpen}
        onOpenChange={(open) => {
          if (!applyingEffects) setEffectsDialogOpen(open);
        }}
      >
        <DialogContent className="max-w-md">
          <DialogHeader>
            <DialogTitle>
              {editingVersion
                ? t('history.versions.editTitle', { name: editingVersion.label })
                : t('history.effectsDialog.title')}
            </DialogTitle>
            <DialogDescription>
              {editingVersion ? t('history.versions.editBody') : t('history.effectsDialog.body')}
            </DialogDescription>
          </DialogHeader>
          {!editingVersion && effectsTargetVersions.length > 1 && (
            <div className="space-y-1.5">
              <label
                htmlFor="history-effects-source"
                className="text-xs font-medium text-muted-foreground"
              >
                {t('history.effectsDialog.sourceLabel')}
              </label>
              <Select
                disabled={applyingEffects}
                value={effectsSourceVersionId ?? ''}
                onValueChange={(val) => setEffectsSourceVersionId(val || null)}
              >
                <SelectTrigger id="history-effects-source" className="h-8 text-xs">
                  <SelectValue placeholder={t('history.effectsDialog.sourcePlaceholder')} />
                </SelectTrigger>
                <SelectContent>
                  {effectsTargetVersions.map((v) => (
                    <SelectItem key={v.id} value={v.id} className="text-xs">
                      {v.label}
                      {v.effects_chain && v.effects_chain.length > 0 && (
                        <span className="text-muted-foreground ml-1.5">
                          ({v.effects_chain.map((e) => e.type).join(' + ')})
                        </span>
                      )}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
          )}
          <fieldset disabled={applyingEffects} className="py-2 max-h-80 overflow-y-auto">
            <EffectsChainEditor value={effectsChain} onChange={setEffectsChain} />
          </fieldset>
          <DialogFooter>
            <Button
              variant="outline"
              disabled={applyingEffects}
              onClick={() => setEffectsDialogOpen(false)}
            >
              {t('common.cancel')}
            </Button>
            <Button
              onClick={handleApplyEffectsConfirm}
              disabled={applyingEffects || (!editingVersion && effectsChain.length === 0)}
            >
              {applyingEffects
                ? t('history.effectsDialog.applying')
                : editingVersion
                  ? t('common.save')
                  : t('history.effectsDialog.apply')}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
