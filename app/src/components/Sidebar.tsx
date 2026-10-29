import { Link, useRouterState } from '@tanstack/react-router';
import {
  AudioLines,
  Box,
  Captions,
  FolderOpen,
  type LucideIcon,
  Mic,
  Settings,
  Volume2,
  Wand2,
} from 'lucide-react';
import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import voiceboxLogo from '@/assets/voicebox-ds-icon.png';
import { Hint } from '@/components/ui/Hint';
import { APP_BRANDING } from '@/lib/constants/branding';
import { cn } from '@/lib/utils/cn';
import { usePlatform } from '@/platform/PlatformContext';
import type { UpdateStatus } from '@/platform/types';
import { version } from '../../package.json';

interface SidebarProps {
  isMacOS?: boolean;
}
type NavItem = { path: string; icon: LucideIcon; labelKey?: string; label?: string };
const createItems: NavItem[] = [
  { path: '/studio/projects', icon: FolderOpen, label: 'Projects' },
  { path: '/', icon: Volume2, labelKey: 'nav.generate' },
  { path: '/voices', icon: Mic, labelKey: 'nav.voices' },
  { path: '/captures', icon: Captions, labelKey: 'nav.captures' },
];
const toolItems: NavItem[] = [
  { path: '/effects', icon: Wand2, labelKey: 'nav.effects' },
  { path: '/models', icon: Box, labelKey: 'nav.models' },
];
const stories: NavItem = { path: '/stories', icon: AudioLines, labelKey: 'nav.stories' };
const settings: NavItem = { path: '/settings', icon: Settings, labelKey: 'nav.settings' };

export function Sidebar({ isMacOS }: SidebarProps) {
  const { t } = useTranslation();
  const path = useRouterState({ select: (state) => state.location.pathname });
  const platform = usePlatform();
  const [updateStatus, setUpdateStatus] = useState<UpdateStatus>(platform.updater.getStatus());
  useEffect(() => platform.updater.subscribe(setUpdateStatus), [platform.updater]);

  const item = (tab: NavItem) => {
    const Icon = tab.icon;
    const label = tab.label ?? t(tab.labelKey!);
    const current =
      tab.path === '/' ? path === '/' : path === tab.path || path.startsWith(`${tab.path}/`);
    return (
      <Hint key={tab.path} content={label}>
        <Link
          to={tab.path}
          activeOptions={{ exact: tab.path === '/' }}
          className="app-rail-link"
          aria-label={label}
          aria-current={current ? 'page' : undefined}
        >
          <Icon className="h-5 w-5 shrink-0" aria-hidden="true" />
          <span className="app-rail-label">{label}</span>
        </Link>
      </Hint>
    );
  };
  return (
    <aside
      className={cn(
        'app-rail fixed left-0 top-0 h-full bg-sidebar border-r border-border flex flex-col py-4 gap-3 overflow-y-auto',
        isMacOS && 'pt-14',
      )}
    >
      <div
        className="px-3 mb-2 shrink-0 flex justify-center"
        title={`${APP_BRANDING.name} · ${APP_BRANDING.edition}`}
      >
        <img
          src={voiceboxLogo}
          alt={`${APP_BRANDING.name} · ${APP_BRANDING.edition}`}
          className="sidebar-logo w-12 h-12 object-contain"
        />
      </div>
      <nav aria-label="Main navigation" className="flex flex-col gap-4 px-2">
        <div className="flex flex-col gap-1">
          <span className="app-rail-group-label">Create</span>
          {createItems.map((tab) => (
            <span key={tab.path}>
              {item(tab)}
              {tab.path === '/studio/projects' &&
                (path === '/stories' || path.startsWith('/stories/')) && (
                  <span className="app-rail-nested">{item(stories)}</span>
                )}
            </span>
          ))}
        </div>
        <div className="flex flex-col gap-1">
          <span className="app-rail-group-label">Tools</span>
          {toolItems.map(item)}
        </div>
      </nav>
      <div className="mt-auto px-2 flex flex-col gap-2">
        {item(settings)}
        <div className="flex flex-col items-center gap-1.5">
          <span className="text-[10px] text-muted-foreground/50">v{version}</span>
          {updateStatus.available && (
            <Link
              to="/settings"
              className="text-[9px] font-semibold tracking-wide uppercase px-2 py-0.5 rounded-full bg-accent/15 text-accent hover:bg-accent/25"
            >
              {t('nav.updateBadge')}
            </Link>
          )}
        </div>
      </div>
    </aside>
  );
}
