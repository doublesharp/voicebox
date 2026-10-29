import {
  cloneElement,
  type ReactElement,
  type ReactNode,
  useEffect,
  useId,
  useRef,
  useState,
} from 'react';
import { createPortal } from 'react-dom';

type HintProps = {
  /** Short supplemental description; the trigger still needs an accessible name. */
  children: ReactElement<{ 'aria-describedby'?: string }>;
  content: ReactNode;
};

/** A hover/focus hint rendered outside clipping ancestors. */
export function Hint({ children, content }: HintProps) {
  const id = useId();
  const anchor = useRef<HTMLSpanElement>(null);
  const hovered = useRef(false);
  const focused = useRef(false);
  const [open, setOpen] = useState(false);
  const [position, setPosition] = useState({ left: 0, top: 0 });

  useEffect(() => {
    if (!open) return;
    const update = () => {
      const rect = anchor.current?.getBoundingClientRect();
      if (rect)
        setPosition({
          left: Math.min(rect.right + 8, window.innerWidth - 180),
          top: rect.top + rect.height / 2,
        });
    };
    const dismiss = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setOpen(false);
    };
    update();
    window.addEventListener('keydown', dismiss);
    window.addEventListener('resize', update);
    window.addEventListener('scroll', update, true);
    return () => {
      window.removeEventListener('keydown', dismiss);
      window.removeEventListener('resize', update);
      window.removeEventListener('scroll', update, true);
    };
  }, [open]);

  const describedBy =
    [children.props['aria-describedby'], open ? id : undefined].filter(Boolean).join(' ') ||
    undefined;
  return (
    // Event delegation keeps the existing focusable trigger and its semantics intact.
    // biome-ignore lint/a11y/noStaticElementInteractions: the child is the interactive element
    <span
      ref={anchor}
      className="app-hint-anchor"
      onMouseEnter={() => {
        hovered.current = true;
        setOpen(true);
      }}
      onMouseLeave={() => {
        hovered.current = false;
        if (!focused.current) setOpen(false);
      }}
      onFocus={() => {
        focused.current = true;
        setOpen(true);
      }}
      onBlur={() => {
        focused.current = false;
        if (!hovered.current) setOpen(false);
      }}
    >
      {cloneElement(children, { 'aria-describedby': describedBy })}
      {open &&
        typeof document !== 'undefined' &&
        createPortal(
          <span id={id} role="tooltip" className="app-hint" style={position}>
            {content}
          </span>,
          document.body,
        )}
    </span>
  );
}
