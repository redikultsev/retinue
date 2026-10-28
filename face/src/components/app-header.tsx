import { FeedCount } from "@/components/feed";
import { LogOut } from "@/components/icons";
import { Button } from "@/components/ui/button";
import { api } from "@/lib/api";
import { type Connection, live, useConnection } from "@/lib/live";
import { cn } from "@/lib/utils";

const WORDS: Record<Connection, string> = {
  idle: "",
  connecting: "подключаюсь…",
  live: "",
  offline: "нет связи",
  outdated: "обновляю приложение…",
};

export function AppHeader() {
  const connection = useConnection();
  return (
    <header className="flex min-h-12 shrink-0 items-center gap-2 border-b px-3 pt-[env(safe-area-inset-top)]">
      <div className="flex min-w-0 flex-1 items-center gap-2">
        <img src="/icons/icon.svg" alt="" className="size-6 rounded-md" />
        <span className="text-[15px] font-semibold">Retinue</span>
        <span
          className={cn("size-1.5 rounded-full", connection === "live" ? "bg-success" : connection === "offline" ? "bg-destructive" : "bg-warning")}
          title={connection === "live" ? "на связи" : WORDS[connection]}
        />
        {WORDS[connection] && <span className="truncate text-xs text-muted-foreground">{WORDS[connection]}</span>}
        <FeedCount />
      </div>
      <Button size="icon" variant="ghost" aria-label="Выйти" title="Выйти" onClick={async () => {
        await api.logout().catch(() => undefined);
        live.stop();
        window.location.assign("/login");
      }}>
        <LogOut />
      </Button>
    </header>
  );
}
