// The Telegram Mini App: its signed `initData` is posted to /api/auth/telegram. Telegram's telegram-web-app.js is
// not loaded (no third-party script): the same string is in the launch URL's hash as `tgWebAppData`.

interface TelegramWindow {
  Telegram?: {
    WebApp?: {
      initData?: string;
      ready?: () => void;
      expand?: () => void;
      close?: () => void;
      openLink?: (url: string) => void;
    };
  };
  /** What Telegram's own apps (iOS, Android, desktop) inject into the web view a Mini App runs in. */
  TelegramWebviewProxy?: { postEvent?: (eventType: string, eventData: string) => void };
}

/** The page runs inside Telegram's Mini App web view (not in a browser, not in the installed PWA). */
export function insideTelegram(win: Window = window): boolean {
  const w = win as unknown as TelegramWindow;
  return typeof w.Telegram?.WebApp?.close === "function" || typeof w.TelegramWebviewProxy?.postEvent === "function";
}

/** Close the Mini App, as telegram-web-app.js does it — without loading that third-party script: the
 * `web_app_close` event posted to the web view. False when there is nothing to close. */
export function closeMiniApp(win: Window = window): boolean {
  const w = win as unknown as TelegramWindow;
  if (typeof w.Telegram?.WebApp?.close === "function") {
    w.Telegram.WebApp.close();
    return true;
  }
  if (typeof w.TelegramWebviewProxy?.postEvent === "function") {
    w.TelegramWebviewProxy.postEvent("web_app_close", "{}");
    return true;
  }
  return false;
}

/** Inside the Mini App a plain link may be swallowed by the web view: the address is handed to Telegram to open in
 * its own browser, as telegram-web-app.js's `openLink` does it (`web_app_open_link`), without that script. False
 * outside Telegram: the link's own `target="_blank"` opens it. */
export function openInTelegram(url: string, win: Window = window): boolean {
  const w = win as unknown as TelegramWindow;
  if (typeof w.Telegram?.WebApp?.openLink === "function") {
    w.Telegram.WebApp.openLink(url);
    return true;
  }
  if (typeof w.TelegramWebviewProxy?.postEvent === "function") {
    w.TelegramWebviewProxy.postEvent("web_app_open_link", JSON.stringify({ url }));
    return true;
  }
  return false;
}

/** The signed init data if the page runs as a Mini App, else null. */
export function telegramInitData(win: Window = window, hash: string = win.location.hash): string | null {
  const fromApi = (win as unknown as TelegramWindow).Telegram?.WebApp?.initData;
  if (fromApi) return fromApi;
  const params = new URLSearchParams(hash.replace(/^#/, ""));
  return params.get("tgWebAppData") || null;
}

/** `startapp=a_<id>` of a t.me/<bot>/<app> link: the approval card the Mini App was opened for. */
export function telegramStartApproval(initData: string | null): string | null {
  if (!initData) return null;
  const start = new URLSearchParams(initData).get("start_param") ?? "";
  const match = /^a_([0-9a-f]{8})$/.exec(start);
  return match?.[1] ?? null;
}

/** A login code from `/login#<code>`: 10 characters, no `=` (that would be Telegram's hash). */
export function codeFromHash(hash: string): string | null {
  let code: string;
  try {
    code = decodeURIComponent(hash.replace(/^#/, "")).trim();
  } catch {
    return null; // a malformed escape such as `#%E0`
  }
  return /^[A-Za-z0-9]{10}$/.test(code) ? code.toUpperCase() : null;
}
