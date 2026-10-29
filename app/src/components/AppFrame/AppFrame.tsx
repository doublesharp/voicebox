import { useRouterState } from '@tanstack/react-router';
import { lazy, Suspense } from 'react';
import { AudioKeepAlive } from '@/components/AudioPlayer/AudioKeepAlive';
import { AudioPlayer } from '@/components/AudioPlayer/AudioPlayer';
import { TitleBarDragRegion } from '@/components/TitleBarDragRegion';
import { TOP_SAFE_AREA_PADDING } from '@/lib/constants/ui';
import { useStory } from '@/lib/hooks/useStories';
import { cn } from '@/lib/utils/cn';
import { useStoryStore } from '@/stores/storyStore';

const StoryTrackEditor = lazy(() =>
  import('@/components/StoriesTab/StoryTrackEditor').then((module) => ({
    default: module.StoryTrackEditor,
  })),
);

interface AppFrameProps {
  children: React.ReactNode;
}

export function AppFrame({ children }: AppFrameProps) {
  const routerState = useRouterState();
  const isStoriesRoute = routerState.location.pathname === '/stories';

  const selectedStoryId = useStoryStore((state) => state.selectedStoryId);
  const { data: story } = useStory(selectedStoryId);

  // Show track editor when on stories route with a selected story that has items
  const showTrackEditor = isStoriesRoute && selectedStoryId && story && story.items.length > 0;

  return (
    <div
      className={cn('h-screen bg-background flex flex-col overflow-hidden', TOP_SAFE_AREA_PADDING)}
    >
      <TitleBarDragRegion />
      <AudioKeepAlive />
      {children}
      {showTrackEditor ? (
        <Suspense fallback={null}>
          <StoryTrackEditor storyId={story.id} items={story.items} />
        </Suspense>
      ) : (
        <AudioPlayer />
      )}
    </div>
  );
}
