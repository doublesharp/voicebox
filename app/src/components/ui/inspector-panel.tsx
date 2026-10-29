import type { ReactNode } from 'react';

export function InspectorPanel({
  title,
  subtitle,
  icon,
  actions,
  footer,
  children,
  overlay = false,
}: {
  title: string;
  subtitle?: string;
  icon?: ReactNode;
  actions?: ReactNode;
  footer: ReactNode;
  children: ReactNode;
  overlay?: boolean;
}) {
  return (
    <section
      className="flex h-full min-h-0 flex-col overflow-hidden bg-card/30"
      style={{ paddingBottom: overlay ? undefined : 'var(--app-player-height, 0px)' }}
    >
      <header
        className={`flex shrink-0 items-center gap-3 border-b border-border/60 px-5 py-4 ${overlay ? 'pr-12' : ''}`}
      >
        {icon && <div className="shrink-0">{icon}</div>}
        <div className="min-w-0 flex-1">
          <h2 className="truncate text-[15px] font-semibold tracking-tight" title={title}>
            {title}
          </h2>
          {subtitle && <p className="mt-1 truncate text-xs text-muted-foreground">{subtitle}</p>}
        </div>
        {actions}
      </header>
      <div className="min-h-0 flex-1 overflow-y-auto">{children}</div>
      <footer className="flex min-h-16 shrink-0 flex-wrap items-center justify-between gap-2 border-t border-border/70 bg-background/95 px-5 py-3">
        {footer}
      </footer>
    </section>
  );
}
