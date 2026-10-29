import { CircleHelp } from 'lucide-react';
import type { ReactNode } from 'react';
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover';

type HelpButtonProps = {
  /** Accessible name describing the subject, for example "About checkpoints". */
  label: string;
  children: ReactNode;
};

export function HelpButton({ label, children }: HelpButtonProps) {
  return (
    <Popover>
      <PopoverTrigger asChild>
        <button type="button" aria-label={label} className="app-help-button">
          <CircleHelp size={16} aria-hidden="true" />
        </button>
      </PopoverTrigger>
      <PopoverContent className="text-sm motion-reduce:animate-none" sideOffset={6}>
        {children}
      </PopoverContent>
    </Popover>
  );
}
