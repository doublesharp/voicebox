import { Link, useRouterState } from '@tanstack/react-router';
import { ChevronLeft, Keyboard } from 'lucide-react';
import { useTranslation } from 'react-i18next';

export function DesktopToolbar() {
  const path = useRouterState({ select: (state) => state.location.pathname });
  const { t } = useTranslation();
  const title = path.startsWith('/studio/')
    ? 'Studio'
    : path.startsWith('/voices')
      ? t('nav.voices')
      : path.startsWith('/captures')
        ? t('nav.captures')
        : path.startsWith('/effects')
          ? t('nav.effects')
          : path.startsWith('/models')
            ? t('nav.models')
            : path.startsWith('/stories')
              ? t('nav.stories')
              : path.startsWith('/settings')
                ? t('nav.settings')
                : t('nav.generate');
  return (
    <div className="desktop-toolbar" role="toolbar" aria-label={`${title} toolbar`}>
      <div className="desktop-toolbar-leading">
        {/^\/studio\/projects\/[^/]+\//.test(path) && (
          <Link
            to="/studio/projects"
            className="desktop-toolbar-button"
            aria-label="Back to projects"
          >
            <ChevronLeft size={16} aria-hidden="true" />
          </Link>
        )}
        <span className="desktop-toolbar-title">{title}</span>
      </div>
      <div className="desktop-toolbar-actions">
        <div id="desktop-toolbar-context-actions" className="desktop-toolbar-context-actions" />
        <button
          type="button"
          className="desktop-toolbar-button"
          aria-label="Keyboard shortcuts"
          onClick={() => window.dispatchEvent(new Event('voicebox:keyboard-shortcuts'))}
        >
          <Keyboard size={16} aria-hidden="true" />
        </button>
      </div>
    </div>
  );
}
