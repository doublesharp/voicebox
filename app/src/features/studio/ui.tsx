import {
  Children,
  cloneElement,
  isValidElement,
  type ReactElement,
  type ReactNode,
  useId,
} from 'react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';

export function StudioButton({
  children,
  onClick,
  disabled,
  primary = false,
  title,
  className = '',
}: {
  children: ReactNode;
  onClick?: () => void;
  disabled?: boolean;
  primary?: boolean;
  title?: string;
  className?: string;
}) {
  return (
    <Button
      type="button"
      size="sm"
      variant={primary ? 'default' : 'outline'}
      disabled={disabled}
      onClick={onClick}
      title={title}
      className={`studio-button ${className}`}
    >
      {children}
    </Button>
  );
}
export function Field({
  label,
  children,
  hint,
}: {
  label: string;
  children: ReactNode;
  hint?: string;
}) {
  const generatedId = useId();
  const first = Children.toArray(children)[0];
  const id =
    isValidElement<{ id?: string }>(first) && first.props.id ? first.props.id : generatedId;
  return (
    <div className="studio-field">
      <label htmlFor={id}>{label}</label>
      {Children.map(children, (child, index) =>
        index === 0 && isValidElement(child)
          ? cloneElement(child as ReactElement<{ id: string; 'aria-describedby'?: string }>, {
              id,
              'aria-describedby': hint ? `${id}-hint` : undefined,
            })
          : child,
      )}
      {hint && <small id={`${id}-hint`}>{hint}</small>}
    </div>
  );
}
export function NumberField({
  label,
  value,
  onCommit,
  min = 0,
  max,
  step = 0.01,
  disabled = false,
}: {
  label: string;
  value: number;
  onCommit: (value: number) => void;
  min?: number;
  max?: number;
  step?: number;
  disabled?: boolean;
}) {
  return (
    <Field label={label}>
      <Input
        key={value}
        aria-label={label}
        type="number"
        defaultValue={Number(value.toFixed(6))}
        min={min}
        max={max}
        step={step}
        disabled={disabled}
        onBlur={(event) => {
          const value = event.currentTarget.valueAsNumber;
          if (Number.isFinite(value) && value >= min && (max === undefined || value <= max))
            onCommit(value);
          else event.currentTarget.value = String(Number(event.currentTarget.defaultValue));
        }}
        onKeyDown={(event) => {
          if (event.key === 'Enter') event.currentTarget.blur();
        }}
      />
    </Field>
  );
}
export function EmptyState({
  title,
  children,
  action,
}: {
  title: string;
  children: ReactNode;
  action?: ReactNode;
}) {
  return (
    <div className="studio-empty">
      <div className="studio-empty-mark" aria-hidden="true">
        ✦
      </div>
      <h2>{title}</h2>
      <p>{children}</p>
      {action}
    </div>
  );
}
