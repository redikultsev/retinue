// `/settings`, «Settings» (docs/face.md «Settings»): what the stack reads and the owner sets — the lane's model and
// effort, the keys, the channels' tokens, the repositories' and the projects' keys — in one place, each row saying
// what it is for and whether it is set. A key is typed once: it goes to the server and is never shown again, not
// here, not to the platform (retinue/settings.py). A change that waits for a restart says so at the top, and the
// owner's «Restart» does it. The computers that reach the collections are listed too, and one is taken back here.
import { useEffect, useState } from "react";
import { PageHeader, PAGE_X } from "@/components/page-header";
import { Check } from "@/components/icons";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { ApiError } from "@/lib/api";
import { when } from "@/lib/format";
import {
  type Device,
  type Lane,
  requestState,
  SERVICE_LABELS,
  type SettingsOp,
  type SettingsView,
  useSettings,
} from "@/lib/settings";
import { toast } from "@/lib/toast";
import { cn, LIST } from "@/lib/utils";

type Send = (op: SettingsOp, about: string, body: Record<string, unknown>) => Promise<string>;
type Revoke = (name: string) => Promise<void>;
type Waiting = Record<string, unknown>;

const SELECT =
  "h-11 w-full min-w-0 rounded-md border border-input bg-transparent px-2 text-base outline-none transition-colors focus-visible:border-ring focus-visible:ring-[3px] focus-visible:ring-ring/50 disabled:opacity-50 md:h-9 md:w-56 md:text-sm";

function errorText(e: unknown): string {
  if (e instanceof ApiError && e.data && typeof e.data === "object" && "error" in e.data) return String(e.data.error);
  return e instanceof Error ? e.message : "The server did not take it.";
}

/** «Set · 7 Nov», «Set», «Not set». */
function stateWords(set: boolean, changed: number | null): string {
  if (!set) return "Not set";
  return changed ? `Set · ${when(changed)}` : "Set";
}

function Section({ title, note, children }: { title: string; note?: React.ReactNode; children: React.ReactNode }) {
  return (
    <section className="flex flex-col gap-2" aria-label={title}>
      <h2 className="px-1 text-[13px] font-medium text-muted-foreground">{title}</h2>
      <ul className={LIST}>{children}</ul>
      {note && <p className="px-1 text-xs text-muted-foreground">{note}</p>}
    </section>
  );
}

/**
 * One secret: its name and what it is for, whether it is set, and «Set» or «Replace», which opens a field under it.
 * The field is a password field that the browser neither fills nor keeps; its text is dropped the moment it is
 * sent, whatever the answer.
 */
function SecretRow({
  title,
  what,
  where,
  hint,
  set,
  changed,
  needed,
  off,
  canClear,
  ready,
  waiting,
  view,
  onSave,
  onClear,
}: {
  title: string;
  what: string;
  where?: string;
  hint: string;
  set: boolean;
  changed: number | null;
  /** Not set and needed: the owner's move (amber). */
  needed: boolean;
  /** Its part is turned off on this server. */
  off?: boolean;
  canClear: boolean;
  ready: boolean;
  waiting: Waiting;
  view: SettingsView | null;
  onSave: (value: string) => Promise<string>;
  onClear?: () => Promise<string>;
}) {
  const [open, setOpen] = useState(false);
  const [value, setValue] = useState("");
  const [sent, setSent] = useState<string | null>(null);
  const [refusal, setRefusal] = useState<string | null>(null);
  const [removing, setRemoving] = useState(false);
  const state = requestState(sent, waiting, view);
  const why = refusal ?? (state === "refused" && sent ? (view?.done[sent]?.why ?? null) : null);

  async function go(call: () => Promise<string>) {
    setValue(""); // gone from the page whatever the server says
    setRefusal(null);
    try {
      setSent(await call());
      setOpen(false);
      setRemoving(false);
    } catch (e) {
      setSent(null);
      setRefusal(errorText(e));
    }
  }

  const status =
    state === "waiting" ? "Saving…" : state === "lost" ? "No answer from the server yet" : stateWords(set, changed);
  return (
    <li data-testid="secret-row" className="flex flex-col gap-3 px-3 py-2.5">
      <div className="flex items-start gap-3">
        <div className="flex min-w-0 flex-1 flex-col gap-0.5">
          {/* the state on the title's line, so what it is for has the row's whole width under it */}
          <div className="flex items-baseline justify-between gap-2">
            <span className="min-w-0 truncate text-[15px] font-medium md:text-[13px]">{title}</span>
            <span
              role="status"
              className={cn(
                "flex shrink-0 items-center gap-1 text-xs tabular-nums",
                needed && !set && state !== "waiting" ? "text-warning" : "text-muted-foreground",
              )}
            >
              {set && state !== "waiting" && <Check className="size-3.5 self-center" />}
              {status}
            </span>
          </div>
          <span className="text-xs text-muted-foreground">
            {what}
            {off && " · turned off on this server"}
          </span>
        </div>
        {/* the same place open or closed: nothing on the row moves */}
        <Button
          size="sm"
          variant="outline"
          className="min-w-20"
          aria-expanded={open}
          disabled={!ready || state === "waiting"}
          onClick={() => {
            setValue("");
            setRefusal(null);
            setRemoving(false);
            setOpen(!open);
          }}
        >
          {open ? "Cancel" : set ? "Replace" : "Set"}
        </Button>
      </div>
      {why && !open && <p className="text-xs text-destructive">{why}</p>}
      {open && (
        <form
          className="flex flex-col gap-2"
          onSubmit={(e) => {
            e.preventDefault();
            const typed = value.trim();
            if (typed) void go(() => onSave(typed));
          }}
        >
          {where && <p className="text-xs text-muted-foreground">{where}</p>}
          <Input
            type="password"
            autoComplete="new-password"
            autoCapitalize="off"
            autoCorrect="off"
            spellCheck={false}
            data-1p-ignore
            data-lpignore="true"
            aria-label={`New ${title}`}
            placeholder={hint}
            value={value}
            onChange={(e) => setValue(e.target.value)}
            autoFocus
            className="font-mono"
          />
          {why && <p className="text-xs text-destructive">{why}</p>}
          <p className="text-xs text-muted-foreground">Saved on the server and never shown again, here or anywhere.</p>
          {removing ? (
            <div className="flex flex-wrap items-center gap-2">
              <span className="text-[13px]">Remove the {title} key from the server? What it does stops after the restart.</span>
              <Button size="sm" variant="destructive" type="button" onClick={() => onClear && void go(onClear)}>
                Remove
              </Button>
              <Button size="sm" variant="ghost" type="button" onClick={() => setRemoving(false)}>
                Keep it
              </Button>
            </div>
          ) : (
            <div className="flex flex-wrap items-center gap-2">
              <Button size="sm" type="submit" disabled={!value.trim()}>
                Save
              </Button>
              {canClear && set && onClear && (
                <Button size="sm" variant="ghost" type="button" className="ml-auto text-destructive" onClick={() => setRemoving(true)}>
                  Remove
                </Button>
              )}
            </div>
          )}
        </form>
      )}
    </li>
  );
}

/** What a device's scope lets it do, in his words. */
const MAY_DO: Record<Device["scope"], string> = {
  read: "Reads your collections",
  write: "Reads and changes your collections, as each one's rights allow",
};

/**
 * One computer: its name, what it may do, when it was last seen, and «Revoke», asked first. Taken back, it stays
 * in the list, greyed, with the day: its name is never given again.
 */
function DeviceRow({ device, revoke }: { device: Device; revoke?: Revoke }) {
  const [asking, setAsking] = useState(false);
  const [busy, setBusy] = useState(false);
  const [why, setWhy] = useState<string | null>(null);
  const gone = device.revoked !== null;
  const status = gone
    ? `Revoked · ${when(device.revoked ?? 0)}`
    : device.seen
      ? `Seen · ${when(device.seen)}`
      : "Not seen yet";
  return (
    <li data-testid="device-row" className={cn("flex flex-col gap-3 px-3 py-2.5", gone && "opacity-60")}>
      <div className="flex items-start gap-3">
        <div className="flex min-w-0 flex-1 flex-col gap-0.5">
          <div className="flex items-baseline justify-between gap-2">
            <span className="min-w-0 truncate text-[15px] font-medium md:text-[13px]">{device.name}</span>
            <span role="status" className="shrink-0 text-xs tabular-nums text-muted-foreground">
              {status}
            </span>
          </div>
          <span className="text-xs text-muted-foreground">{MAY_DO[device.scope]}</span>
        </div>
        {!gone && revoke && (
          <Button
            size="sm"
            variant="outline"
            className="min-w-20"
            aria-expanded={asking}
            disabled={asking || busy}
            onClick={() => {
              setWhy(null);
              setAsking(true);
            }}
          >
            Revoke
          </Button>
        )}
      </div>
      {asking && revoke && (
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-[13px]">
            Take {device.name} back? Its token stops working at its next call; a new one needs a new name.
          </span>
          <Button
            size="sm"
            variant="destructive"
            disabled={busy}
            onClick={async () => {
              setBusy(true);
              try {
                await revoke(device.name);
                setAsking(false);
              } catch (e) {
                setWhy(errorText(e));
              } finally {
                setBusy(false);
              }
            }}
          >
            Yes, revoke
          </Button>
          <Button size="sm" variant="ghost" disabled={busy} onClick={() => setAsking(false)}>
            Keep it
          </Button>
        </div>
      )}
      {why && <p className="text-xs text-destructive">{why}</p>}
    </li>
  );
}

function DevicesSection({ devices, revoke }: { devices: Device[]; revoke?: Revoke }) {
  // the ones that still reach the collections first, each group as made
  const shown = [...devices.filter((d) => d.revoked === null), ...devices.filter((d) => d.revoked !== null)];
  return (
    <Section
      title="Devices"
      note={
        <>
          A computer is added on the server's terminal, where its token is shown once:
          <code className="mt-1 block rounded-md bg-muted px-2 py-1.5 font-mono break-words">
            <span className="whitespace-nowrap">retinue-platform</span> collections device add &lt;name&gt;{" "}
            <span className="whitespace-nowrap">--scope read|write</span>
          </code>
        </>
      }
    >
      {shown.length ? (
        shown.map((d) => <DeviceRow key={d.name} device={d} revoke={revoke} />)
      ) : (
        <li className="px-3 py-2.5 text-xs text-muted-foreground">No computer reaches your collections yet.</li>
      )}
    </Section>
  );
}

/** A row of the lane's: a label, and its control or value at the right. */
function FieldRow({ label, children, note }: { label: string; children: React.ReactNode; note?: string }) {
  return (
    <li className="flex flex-col gap-1.5 px-3 py-2.5 md:flex-row md:items-center md:gap-3">
      <div className="flex min-w-0 flex-1 flex-col gap-0.5">
        <span className="text-[15px] font-medium md:text-[13px]">{label}</span>
        {note && <span className="text-xs text-muted-foreground">{note}</span>}
      </div>
      {children}
    </li>
  );
}

function LaneSection({
  lane,
  view,
  many,
  ready,
  waiting,
  send,
}: {
  lane: Lane;
  view: SettingsView;
  many: boolean;
  ready: boolean;
  waiting: Waiting;
  send: Send;
}) {
  const [sent, setSent] = useState<Record<string, string>>({});
  // What he picked, shown until the host has answered: the list never jumps back while it saves.
  const [picked, setPicked] = useState<Record<string, string>>({});
  const [refusal, setRefusal] = useState<string | null>(null);
  async function change(field: "model" | "effort", value: string) {
    setRefusal(null);
    setPicked((p) => ({ ...p, [field]: value }));
    setSent(({ [field]: _earlier, ...rest }) => rest); // until this one has its id, no older answer counts
    try {
      const id = await send("lane", `lane:${lane.name}:${field}`, { lane: lane.name, [field]: value });
      setSent((s) => ({ ...s, [field]: id }));
    } catch (e) {
      setPicked(({ [field]: _gone, ...rest }) => rest);
      setRefusal(errorText(e));
    }
  }
  const shown = (field: "model" | "effort") => {
    const id = sent[field];
    const state = requestState(id ?? null, waiting, view);
    return field in picked && (state === "waiting" || (state === "none" && !id)) ? picked[field] : lane[field];
  };
  const note = (field: "model" | "effort") => {
    const state = requestState(sent[field] ?? null, waiting, view);
    if (state === "waiting") return "Saving…";
    if (state === "refused") return view.done[sent[field] ?? ""]?.why;
    if (lane[field] !== lane.running[field]) return "After the restart";
    return undefined;
  };
  return (
    <Section
      title={many ? `Sessions · ${lane.name}` : "Sessions"}
      note="Every session runs with these. A change applies from each session's next turn after the restart."
    >
      <FieldRow label="Engine">
        <span className="text-[13px] text-muted-foreground">{lane.engine_label}</span>
      </FieldRow>
      <FieldRow label="Model" note={note("model")}>
        <select
          aria-label="Model"
          className={SELECT}
          value={shown("model")}
          disabled={!ready}
          onChange={(e) => void change("model", e.target.value)}
        >
          {view.models.map((m) => (
            <option key={m.id} value={m.id}>
              {m.label}
            </option>
          ))}
        </select>
      </FieldRow>
      <FieldRow label="Effort" note={note("effort")}>
        <select
          aria-label="Effort"
          className={SELECT}
          value={shown("effort")}
          disabled={!ready}
          onChange={(e) => void change("effort", e.target.value)}
        >
          {view.efforts.map((m) => (
            <option key={m.id} value={m.id}>
              {m.label}
            </option>
          ))}
        </select>
      </FieldRow>
      {refusal && (
        <li className="px-3 py-2 text-xs text-destructive">{refusal}</li>
      )}
    </Section>
  );
}

/** «Restart to apply», while a saved change waits: what restarts, and the one button. */
function RestartBar({ view, waiting, send }: { view: SettingsView; waiting: Waiting; send: Send }) {
  const [sent, setSent] = useState<string | null>(null);
  const [refusal, setRefusal] = useState<string | null>(null);
  const state = requestState(sent, waiting, view);
  const restarting = state === "waiting" || view.restarting.length > 0;
  const why = refusal ?? (state === "refused" && sent ? view.done[sent]?.why : null);
  // the bar goes when it is over; a quiet word says it went well
  useEffect(() => {
    if (state === "done") toast("Restarted: the changes are in use");
  }, [state]);
  if (!restarting && view.pending.length === 0 && !why) return null;
  const what = (restarting && view.restarting.length ? view.restarting : view.pending).map((s) => SERVICE_LABELS[s] ?? s);
  return (
    <div role="status" className="flex flex-col gap-2 rounded-lg border px-3 py-2.5 md:flex-row md:items-center md:gap-3">
      <span className={cn("hidden size-2 shrink-0 rounded-full md:block", restarting ? "bg-primary" : "bg-warning")} />
      <div className="flex min-w-0 flex-1 flex-col gap-0.5">
        <span className="text-[13px] font-medium">
          {restarting ? "Restarting…" : why ? "The restart did not happen" : "Restart to apply your changes"}
        </span>
        <span className={cn("text-xs", why && !restarting ? "text-destructive" : "text-muted-foreground")}>
          {restarting
            ? "The app reconnects by itself in a minute."
            : why
              ? why
              : `Restarts ${what.join(", ")}. A running turn stops and goes on by itself.`}
        </span>
      </div>
      {!restarting && view.pending.length > 0 && (
        <Button
          size="sm"
          onClick={async () => {
            setRefusal(null);
            try {
              setSent(await send("restart", "restart", {}));
            } catch (e) {
              setRefusal(errorText(e));
              toast(errorText(e), "error");
            }
          }}
        >
          Restart
        </Button>
      )}
    </div>
  );
}

export function SettingsBody({
  view,
  waiting,
  send,
  revoke,
}: {
  view: SettingsView;
  waiting: Waiting;
  send: Send;
  revoke?: Revoke;
}) {
  const ready = view.ready;
  const keysOf = (group: "keys" | "channels") =>
    view.keys
      .filter((k) => k.group === group)
      .map((k) => (
        <SecretRow
          key={k.name}
          title={k.title}
          what={k.what}
          where={k.where}
          hint={k.hint}
          set={k.set}
          changed={k.changed}
          needed={!k.clear && k.on}
          off={!k.on}
          canClear={k.clear}
          ready={ready}
          waiting={waiting}
          view={view}
          onSave={(value) => send("key", k.name, { name: k.name, value })}
          onClear={() => send("key", k.name, { name: k.name, clear: true })}
        />
      ));
  return (
    <>
      {!ready && (
        <div className="flex flex-col gap-2 rounded-lg border px-3 py-2.5">
          <span className="text-[13px] font-medium">Settings cannot change anything on this server yet</span>
          <span className="text-xs text-muted-foreground">
            Its service on the server is not installed. Run setup.sh there once more, with the switches you installed
            with:
          </span>
          <code className="rounded-md bg-muted px-2 py-1.5 font-mono text-xs break-all">
            sudo bash /opt/retinue/deploy/setup.sh
          </code>
        </div>
      )}
      <RestartBar view={view} waiting={waiting} send={send} />
      {view.lanes.map((lane) => (
        <LaneSection
          key={lane.name}
          lane={lane}
          view={view}
          many={view.lanes.length > 1}
          ready={ready}
          waiting={waiting}
          send={send}
        />
      ))}
      <Section title="Keys">{keysOf("keys")}</Section>
      <Section title="Channels">{keysOf("channels")}</Section>
      {view.devices && <DevicesSection devices={view.devices} revoke={revoke} />}
      {view.git.length > 0 && (
        <Section
          title="Repositories"
          note="A GitHub token is fine-grained: only these repositories, Contents and Pull requests read and write."
        >
          {view.git.map((g) => (
            <SecretRow
              key={g.key}
              title={g.key}
              what={g.repos.length ? `Fetches and pushes ${g.repos.join(", ")}` : "No repository in platform.yaml uses it"}
              where="GitHub → Settings → Developer settings → Fine-grained tokens, or a GitLab access token with read and write of the repository."
              hint="github_pat_…"
              set={g.set}
              changed={g.changed}
              needed={g.repos.length > 0}
              canClear={false}
              ready={ready}
              waiting={waiting}
              view={view}
              onSave={(value) => send("git", `git:${g.key}`, { key: g.key, value })}
            />
          ))}
        </Section>
      )}
      {view.project_keys.length > 0 && (
        <Section title="Projects" note="Sent by the server with a project's calls to its own hosts; its sessions never see it.">
          {view.project_keys.map((p) => (
            <SecretRow
              key={p.name}
              title={p.project ? `${p.project}` : p.name}
              what={p.project ? `The key of the project's own hosts (${p.name})` : "No project's list uses it"}
              hint="the key the site gave you"
              set={p.set}
              changed={p.changed}
              needed={Boolean(p.project)}
              canClear={false}
              ready={ready}
              waiting={waiting}
              view={view}
              onSave={(value) => send("project_key", `project:${p.name}`, { name: p.name, value })}
            />
          ))}
        </Section>
      )}
    </>
  );
}

export function SettingsPage() {
  const { view, error, waiting, load, send, revoke } = useSettings();
  return (
    <div className="flex h-full min-h-0 flex-1 flex-col">
      <PageHeader title="Settings" />
      <main
        className={cn(
          "mx-auto flex w-full max-w-3xl min-h-0 flex-1 flex-col gap-6 overflow-y-auto pt-4 pb-[calc(env(safe-area-inset-bottom)+1rem)]",
          PAGE_X,
        )}
      >
        {view ? (
          <SettingsBody view={view} waiting={waiting} send={send} revoke={revoke} />
        ) : error ? (
          <div className="flex flex-col items-start gap-2">
            <p className="text-sm text-destructive">{error}</p>
            <Button size="sm" variant="outline" onClick={() => void load()}>
              Try again
            </Button>
          </div>
        ) : (
          <p className="text-sm text-muted-foreground">Loading…</p>
        )}
      </main>
    </div>
  );
}
