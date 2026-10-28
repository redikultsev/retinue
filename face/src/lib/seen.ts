// «Непрочитано» (retinue/sessions.py `mark_seen`): a session open on a visible screen tells the platform the owner
// has seen it — when it opens, when something new comes while it is open, and when the app comes back to the front.
import { useEffect, useState } from "react";
import { api } from "./api";
import { live } from "./live";

export function useMarkSeen(id: string, unread: boolean, seen: typeof api.seen = api.seen): void {
  const [visible, setVisible] = useState(() => typeof document === "undefined" || !document.hidden);
  useEffect(() => {
    const onVisibility = () => setVisible(!document.hidden);
    document.addEventListener("visibilitychange", onVisibility);
    return () => document.removeEventListener("visibilitychange", onVisibility);
  }, []);
  useEffect(() => {
    if (!unread || !visible) return;
    seen(id)
      .then((res) => live.dispatch({ type: "event", id: null, event: { kind: "session", data: res.session } }))
      .catch(() => undefined); // the dot stays; the next look tries again
  }, [id, unread, visible, seen]);
}
