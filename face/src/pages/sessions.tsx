// `/sessions`: the sessions by sphere (components/session-list.tsx), the phone's second tab. On a wide screen the list
// is the left column already, so the middle only says so.
import { AppHeader } from "@/components/app-header";
import { SessionList } from "@/components/session-list";
import { useWide } from "@/lib/media";

export function SessionsPage() {
  const wide = useWide();
  if (wide)
    return (
      <div className="flex h-full items-center justify-center p-6">
        <p className="text-sm text-muted-foreground">Выбери сессию слева.</p>
      </div>
    );
  return (
    <div className="flex h-full min-h-0 flex-col">
      <AppHeader />
      <main className="min-h-0 flex-1 overflow-y-auto px-2 pb-2">
        <SessionList />
      </main>
    </div>
  );
}
