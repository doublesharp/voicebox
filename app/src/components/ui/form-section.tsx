import { type ReactNode, useId } from 'react';
import { cn } from '@/lib/utils/cn';

/** A shared visual and accessible group for inspector and dialog forms. */
export function FormSection({
  title,
  description,
  children,
  className,
  action,
}: {
  title: string;
  description?: string;
  children: ReactNode;
  className?: string;
  action?: ReactNode;
}) {
  const id = useId();
  return (
    <section aria-labelledby={id} className={cn('form-section space-y-4', className)}>
      <header className="flex items-start justify-between gap-3">
        <div className="min-w-0 space-y-1">
          <h3 id={id} className="text-sm font-semibold tracking-tight text-foreground">
            {title}
          </h3>
          {description && (
            <p className="text-xs leading-relaxed text-muted-foreground">{description}</p>
          )}
        </div>
        {action && <div className="shrink-0">{action}</div>}
      </header>
      {children}
    </section>
  );
}
