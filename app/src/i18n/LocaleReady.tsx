import { type ReactNode, useEffect, useState } from 'react';
import i18n, { i18nReady } from './index';

/** Keep the first frame in the selected language, including the dictate view. */
export function LocaleReady({ children }: { children: ReactNode }) {
  const [ready, setReady] = useState(i18n.isInitialized);
  useEffect(() => {
    let active = true;
    void i18nReady.then(() => {
      if (active) setReady(true);
    });
    return () => {
      active = false;
    };
  }, []);
  return ready ? children : null;
}
