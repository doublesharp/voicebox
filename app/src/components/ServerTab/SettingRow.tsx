import type { ReactNode } from 'react';

/**
 * A section header with title and optional description, separated by a border.
 */
export function SettingSection({
  title,
  description,
  children,
}: {
  title?: string;
  description?: string;
  children: ReactNode;
}) {
  return (
    <section className="space-y-2.5">
      {title && <h3 className="text-sm font-semibold tracking-tight">{title}</h3>}
      {description && (
        <p className="text-xs leading-relaxed text-muted-foreground">{description}</p>
      )}
      <div className="overflow-hidden rounded-xl border border-border/70 bg-card/50 px-4 divide-y divide-border/50">
        {children}
      </div>
    </section>
  );
}

/**
 * A single settings row: label+description on the left, action on the right.
 * Use for toggles, inputs, buttons, badges — any control type.
 */
export function SettingRow({
  title,
  description,
  htmlFor,
  action,
  children,
}: {
  title: string;
  description?: string;
  htmlFor?: string;
  /** Right-aligned control (checkbox, button, badge, etc.) */
  action?: ReactNode;
  /** Full-width content rendered below the label row (for sliders, inputs, etc.) */
  children?: ReactNode;
}) {
  return (
    <div className="py-4">
      <div className="flex flex-wrap items-center justify-between gap-x-5 gap-y-3">
        <div className="min-w-0">
          <label
            htmlFor={htmlFor}
            className={`text-sm font-medium leading-5 select-none ${htmlFor ? 'cursor-pointer' : ''}`}
          >
            {title}
          </label>
          {description && (
            <p className="text-xs leading-relaxed text-muted-foreground mt-1 break-words">
              {description}
            </p>
          )}
        </div>
        {action && <div className="shrink-0">{action}</div>}
      </div>
      {children && <div className="mt-3">{children}</div>}
    </div>
  );
}
