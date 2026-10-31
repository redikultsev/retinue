// `/login`: the one-time code the bot sends for `/app` (typed, or taken from `/login#<code>`), or, inside the
// Telegram Mini App, its signed init data — no code needed.
import { useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { ApiError, api } from "@/lib/api";
import { codeFromHash, telegramInitData, telegramStartApproval } from "@/lib/telegram";

interface Props {
  next?: string;
  loginCode?: typeof api.loginCode;
  loginTelegram?: typeof api.loginTelegram;
  /** Where to go once logged in; a full load by default, so the app starts with the new cookie. */
  done?: (to: string) => void;
}

/** Where to go after login: a path of this app only. Resolved as the browser would resolve it, so `//evil`,
 * `/\\evil`, `/%09/evil` and the like cannot leave the origin; anything else is `/`. */
export function safeNext(next: string | undefined, origin: string = window.location.origin): string {
  if (!next?.startsWith("/")) return "/";
  let url: URL;
  try {
    url = new URL(next, origin);
  } catch {
    return "/";
  }
  const path = `${url.pathname}${url.search}${url.hash}`;
  if (url.origin !== origin || !path.startsWith("/") || path.startsWith("//") || path.startsWith("/\\")) return "/";
  return url.pathname.startsWith("/login") ? "/" : path;
}

export function LoginPage({ next, loginCode = api.loginCode, loginTelegram = api.loginTelegram, done }: Props) {
  const finish = done ?? ((to: string) => window.location.replace(to));
  const [code, setCode] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [telegram, setTelegram] = useState(false);
  const tried = useRef(false);

  async function submit(value: string) {
    setBusy(true);
    setError(null);
    try {
      await loginCode(value.trim().toUpperCase());
      finish(safeNext(next));
    } catch (e) {
      setError(e instanceof ApiError && e.status !== 401 ? e.message : "The code did not work: it is single-use and lives 10 minutes. Ask for a new one: /app.");
      setBusy(false);
    }
  }

  // biome-ignore lint/correctness/useExhaustiveDependencies: runs once, on the URL the page was opened with
  useEffect(() => {
    if (tried.current) return;
    tried.current = true;
    const initData = telegramInitData();
    if (initData) {
      setTelegram(true);
      setBusy(true);
      loginTelegram(initData)
        .then(() => {
          const card = telegramStartApproval(initData);
          finish(card ? `/a/${card}` : safeNext(next));
        })
        .catch((e) => {
          setError(e instanceof ApiError && e.status !== 401 ? e.message : "Telegram did not confirm the login. Open the app from the bot again.");
          setBusy(false);
        });
      return;
    }
    const fromHash = codeFromHash(window.location.hash);
    if (fromHash) {
      history.replaceState(null, "", window.location.pathname + window.location.search); // the code leaves the URL
      setCode(fromHash);
      void submit(fromHash);
    }
  }, []);

  return (
    <main className="mx-auto flex min-h-dvh w-full max-w-sm flex-col justify-center gap-6 px-4 pb-[env(safe-area-inset-bottom)]">
      <div className="flex flex-col items-center gap-3 text-center">
        <img src="/icons/icon.svg" alt="" className="size-16 rounded-2xl" />
        <h1 className="text-2xl font-semibold">Retinue</h1>
        <p className="text-sm text-muted-foreground">
          {telegram ? "Logging in through Telegram…" : "Send the bot /app — it replies with a link and a 10-character code."}
        </p>
      </div>
      {!telegram && (
        <form
          className="flex flex-col gap-3"
          onSubmit={(e) => {
            e.preventDefault();
            void submit(code);
          }}
        >
          <label className="flex flex-col gap-1">
            <span className="text-sm font-medium">Login code</span>
            <Input
              value={code}
              onChange={(e) => setCode(e.target.value)}
              autoComplete="one-time-code"
              autoCapitalize="characters"
              autoCorrect="off"
              spellCheck={false}
              maxLength={10}
              inputMode="text"
              placeholder="XXXXXXXXXX"
              className="h-12 text-center font-mono text-xl tracking-[0.3em] md:h-12 md:text-xl"
            />
          </label>
          <Button type="submit" size="lg" disabled={busy || code.trim().length !== 10}>
            {busy ? "Logging in…" : "Log in"}
          </Button>
        </form>
      )}
      {error && (
        <p role="alert" className="text-center text-sm text-destructive">
          {error}
        </p>
      )}
    </main>
  );
}
