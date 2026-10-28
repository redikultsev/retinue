// `/new` in a router of its own, as the tests need it: the page sends, then opens the session it made.
import { createMemoryHistory, createRootRoute, createRoute, createRouter, RouterProvider } from "@tanstack/react-router";
import { act, render } from "@testing-library/react";
import { NewPage } from "@/pages/new";

export async function mountNew(props: React.ComponentProps<typeof NewPage> = {}) {
  const root = createRootRoute();
  const page = createRoute({ getParentRoute: () => root, path: "/new", component: () => <NewPage {...props} /> });
  const today = createRoute({ getParentRoute: () => root, path: "/today", component: () => <p>сегодня</p> });
  const one = createRoute({ getParentRoute: () => root, path: "/s/$id", component: () => <p>открыта сессия</p> });
  const router = createRouter({
    routeTree: root.addChildren([page, today, one]),
    history: createMemoryHistory({ initialEntries: ["/new"] }),
  });
  const view = render(<RouterProvider router={router} />);
  await act(async () => {
    await router.load();
  });
  return { router, view };
}
