// The routes (TanStack Router, code-based: no generator plugin).
import {
  createRootRoute,
  createRoute,
  createRouter,
  type RouterHistory,
  Outlet,
  redirect,
  useRouterState,
} from "@tanstack/react-router";
import { useEffect, useMemo, useRef } from "react";
import { Shell } from "@/components/shell";
import { Toaster } from "@/components/toaster";
import { feedSource, useFeed } from "@/lib/feed";
import { live, useLiveState } from "@/lib/live";
import { cardNotice, visibleSessions } from "@/lib/notify";
import { toast } from "@/lib/toast";
import { waitingCount } from "@/lib/store";
import { ApprovalPage } from "@/pages/approval";
import { BasePage } from "@/pages/base";
import { PushPage } from "@/pages/push";
import { FilePage } from "@/pages/file";
import { LoginPage } from "@/pages/login";
import { NewPage } from "@/pages/new";
import { ReadPage } from "@/pages/read";
import { type SessionSearch, SessionPage } from "@/pages/session";
import { SessionsPage } from "@/pages/sessions";
import { RecordPage, SpacePage, SpacesPage } from "@/pages/spaces";
import { TodayPage } from "@/pages/today";
import { WeekPage } from "@/pages/week";
import { visits } from "@/lib/today";

const tabOf = (v: unknown) => (v === "changes" || v === "chat" || v === "files" ? v : undefined);
const str = (v: unknown) => (typeof v === "string" && v ? v : undefined);

/** `(2) Retinue` and the app badge: what waits for the owner — the feed's rows, the cards until it has loaded. */
function useWaitingTitle() {
  const { sessions } = useLiveState();
  const feed = useFeed();
  const cards = useMemo(() => waitingCount(Object.values(sessions)), [sessions]);
  const n = feed ? feed.count : cards;
  useEffect(() => {
    document.title = n > 0 ? `(${n}) Retinue` : "Retinue";
    const nav = navigator as Navigator & { setAppBadge?: (n: number) => Promise<void>; clearAppBadge?: () => Promise<void> };
    if (n > 0) nav.setAppBadge?.(n).catch(() => undefined);
    else nav.clearAppBadge?.().catch(() => undefined);
  }, [n]);
}

/** A card for a session not on screen: a quiet toast that opens it. */
function useCardNotices() {
  const location = useRouterState({ select: (s) => s.location });
  const where = useRef(location);
  where.current = location;
  useEffect(
    () =>
      live.onNewCard((approval) => {
        const { pathname, searchStr } = where.current;
        const notice = cardNotice(
          approval,
          live.getState().sessions[approval.session],
          visibleSessions(pathname, searchStr),
          pathname,
        );
        if (notice) toast(notice.text, "info", 8000, notice.href);
      }),
    [],
  );
}

function Root() {
  const path = useRouterState({ select: (s) => s.location.pathname });
  const login = path.startsWith("/login");
  useEffect(() => {
    if (login) return;
    void live.start();
    feedSource.start(live);
    const onVisibility = () => {
      live.visibility(document.hidden);
      if (document.hidden) visits.hidden();
      else visits.shown();
    };
    const onLeave = () => visits.hidden();
    document.addEventListener("visibilitychange", onVisibility);
    window.addEventListener("pagehide", onLeave);
    return () => {
      document.removeEventListener("visibilitychange", onVisibility);
      window.removeEventListener("pagehide", onLeave);
      feedSource.stop();
    };
  }, [login]);
  useWaitingTitle();
  useCardNotices();
  return (
    <>
      <Outlet />
      <Toaster />
    </>
  );
}

const rootRoute = createRootRoute({ component: Root });

// The start is «Today»: `/` — an old link, a bookmark, the PWA's start_url, the bot's menu button — goes there. The
// hash goes along: a Mini App's signed data is in it (`tgWebAppData`), and the login needs it.
const homeRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/",
  beforeLoad: ({ location }) => {
    throw redirect({ to: "/today", hash: location.hash || undefined, replace: true });
  },
});

// The tabs and the sessions, inside the shell (the tab bar on a phone; the sidebar and the right pane on a desktop).
const shellRoute = createRoute({
  getParentRoute: () => rootRoute,
  id: "shell",
  component: () => (
    <Shell>
      <Outlet />
    </Shell>
  ),
});

const todayRoute = createRoute({ getParentRoute: () => shellRoute, path: "/today", component: TodayPage });
const sessionsRoute = createRoute({ getParentRoute: () => shellRoute, path: "/sessions", component: SessionsPage });
// «＋»: a new task, where a session would be (pages/new.tsx).
const newRoute = createRoute({ getParentRoute: () => shellRoute, path: "/new", component: () => <NewPage /> });
const pagesRoute = createRoute({ getParentRoute: () => shellRoute, path: "/pages", component: ReadPage });
// «Pages» was «Read»: an old link or bookmark still lands on it.
const readRoute = createRoute({
  getParentRoute: () => shellRoute,
  path: "/read",
  beforeLoad: () => {
    throw redirect({ to: "/pages", replace: true });
  },
});
const weekRoute = createRoute({ getParentRoute: () => shellRoute, path: "/week", component: () => <WeekPage /> });
const spacesRoute = createRoute({ getParentRoute: () => shellRoute, path: "/spaces", component: SpacesPage });
const spaceRoute = createRoute({
  getParentRoute: () => shellRoute,
  path: "/spaces/$space",
  component: function Space() {
    const { space } = spaceRoute.useParams();
    return <SpacePage key={space} space={space} />;
  },
});
const recordRoute = createRoute({
  getParentRoute: () => shellRoute,
  path: "/spaces/$space/r/$",
  component: function Record() {
    const { space, _splat } = recordRoute.useParams();
    return <RecordPage key={`${space}/${_splat}`} space={space} path={_splat ?? ""} />;
  },
});

const loginRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/login",
  validateSearch: (s: Record<string, unknown>): { next?: string } => ({ next: str(s.next) }),
  component: function Login() {
    const { next } = loginRoute.useSearch();
    return <LoginPage next={next} />;
  },
});

const sessionRoute = createRoute({
  getParentRoute: () => shellRoute,
  path: "/s/$id",
  validateSearch: (s: Record<string, unknown>): SessionSearch => ({
    with: str(s.with),
    tab: tabOf(s.tab),
    tab2: tabOf(s.tab2),
    pane: str(s.pane),
  }),
  component: function Session() {
    const { id } = sessionRoute.useParams();
    const search = sessionRoute.useSearch();
    return <SessionPage key={id} id={id} search={search} />;
  },
});

const approvalRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/a/$id",
  component: function Approval() {
    const { id } = approvalRoute.useParams();
    return <ApprovalPage id={id} />;
  },
});

export function parseSearch(search: string): Record<string, string> {
  return Object.fromEntries(new URLSearchParams(search.replace(/^\?/, "")));
}

export function stringifySearch(search: Record<string, unknown>): string {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(search)) if (value !== undefined && value !== null) params.set(key, String(value));
  const query = params.toString();
  return query ? `?${query}` : "";
}

const fileRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/f/$id",
  component: function File() {
    const { id } = fileRoute.useParams();
    return <FilePage id={id} />;
  },
});

const baseRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/b/$id",
  component: function Base() {
    const { id } = baseRoute.useParams();
    return <BasePage id={id} />;
  },
});

const pushRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/p/$id",
  component: function Push() {
    const { id } = pushRoute.useParams();
    return <PushPage id={id} />;
  },
});

const routeTree = rootRoute.addChildren([
  homeRoute,
  loginRoute,
  shellRoute.addChildren([todayRoute, sessionsRoute, newRoute, pagesRoute, readRoute, weekRoute, spacesRoute, spaceRoute, recordRoute, sessionRoute]),
  approvalRoute,
  fileRoute,
  baseRoute,
  pushRoute,
]);

/** The app's router; a test passes its own (memory) history. */
export function makeRouter(history?: RouterHistory) {
  return createRouter({
    routeTree,
    ...(history ? { history } : {}),
    // Search values stay plain strings: the default parsing would turn a session id like 77889900 into a number.
    parseSearch: parseSearch,
    stringifySearch: stringifySearch,
    defaultNotFoundComponent: () => <p className="p-6 text-sm text-muted-foreground">There is no such page.</p>,
  });
}

export const router = makeRouter();

declare module "@tanstack/react-router" {
  interface Register {
    router: typeof router;
  }
}
