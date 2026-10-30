import { QueryClientProvider } from '@tanstack/react-query';
import React from 'react';
import ReactDOM from 'react-dom/client';
// import { ReactQueryDevtools } from '@tanstack/react-query-devtools';
import App from '@/App';
import { LocaleReady } from '@/i18n/LocaleReady';
// Import CSS from app directory using alias so Tailwind can scan the source files
import '@/index.css';
import { queryClient } from '@/lib/queryClient';
import { PlatformProvider } from '@/platform/PlatformContext';
import { tauriPlatform } from './platform';
import { installDesktopMenu } from './platform/desktopMenu';

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <QueryClientProvider client={queryClient}>
      <PlatformProvider platform={tauriPlatform}>
        <LocaleReady>
          <App />
        </LocaleReady>
        {/* <ReactQueryDevtools initialIsOpen={false} /> */}
      </PlatformProvider>
    </QueryClientProvider>
  </React.StrictMode>,
);

// The dictate overlay must not replace the main window's application menu.
const isDictate = new URLSearchParams(window.location.search).get('view') === 'dictate';
const mac = /mac/i.test(navigator.platform);
const windows = /windows/i.test(navigator.userAgent);
if (!isDictate && (mac || windows)) {
  void installDesktopMenu(mac).catch((error) =>
    console.error('Could not install desktop menu:', error),
  );
}
