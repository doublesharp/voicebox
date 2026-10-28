import '@/features/studio/visuals.css';
import doubleSharpLogo from '@/assets/doublesharp-logo.png';
import { APP_BRANDING } from '@/lib/constants/branding';
import { cn } from '@/lib/utils/cn';

export function DoubleSharpMark({ className }: { className?: string }) {
  return (
    <span
      aria-hidden="true"
      className={cn(
        'doublesharp-pulse relative inline-block h-7 w-7 shrink-0 overflow-hidden rounded-full bg-black',
        className,
      )}
    >
      <img
        src={doubleSharpLogo}
        alt=""
        className="absolute top-0 left-0 h-full w-auto max-w-none"
      />
    </span>
  );
}

export function EditionBadge({ className }: { className?: string }) {
  return (
    <span
      className={cn(
        'inline-flex items-center gap-2 rounded-full border border-accent/25 bg-accent/5 px-3 py-1 text-[9px] font-medium uppercase tracking-[0.18em] text-foreground',
        className,
      )}
    >
      <span
        aria-hidden="true"
        className="h-1 w-4 rounded-full"
        style={{ background: 'var(--brand-gradient)' }}
      />
      {APP_BRANDING.edition}
    </span>
  );
}

export function DoubleSharpSignature({ className }: { className?: string }) {
  return (
    <a
      href={APP_BRANDING.websiteUrl}
      target="_blank"
      rel="noopener noreferrer"
      aria-label={`${APP_BRANDING.attribution} · DoubleSharp website`}
      className={cn(
        'inline-flex w-64 max-w-full flex-col items-start gap-1 rounded-xl bg-black px-4 py-3 ring-1 ring-inset ring-white/10 transition-opacity hover:opacity-80 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
        className,
      )}
    >
      <span className="text-[9px] font-medium uppercase tracking-[0.22em] text-white/70">
        Enhanced by
      </span>
      <img src={doubleSharpLogo} alt="DoubleSharp" className="block h-auto w-full" />
    </a>
  );
}
