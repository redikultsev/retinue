// The owner's writes from a Space's screen (docs/collections.md#from-the-screen). `Editor` holds them for the views
// inside it: a write at the version shown, «Undo» in its toast; a record moved on since asks «Apply mine» or «Keep»; a
// record that may be one already there asks «Add anyway»; a record's sheet — its fields, «Move to» a board's other
// column, its changes each with «Undo», «Delete» — and the sheet that adds one by the view's `add`. Without an Editor
// the views are read only.
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { Dialog } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Plus } from "@/components/icons";
import { ApiError } from "@/lib/api";
import type { AddSpec, FieldSpec, FormAnswer, RecordAnswer, Refusal } from "@/lib/collections";
import { valueWords } from "@/lib/collections";
import {
  actorWords,
  addFields,
  againAt,
  ago,
  changeWords,
  conflictWords,
  type EditApi,
  editApi,
  recordWords,
  refusalOf,
  type Step,
  startValues,
  undoSteps,
  valueText,
} from "@/lib/edit";
import { toast } from "@/lib/toast";
import { cn, inputId } from "@/lib/utils";

/** How long a write's toast keeps its «Undo». */
export const UNDO_MS = 8000;

/** A board's column a record may move to, as its sheet offers it. */
export interface Move {
  field: string;
  values: (string | null)[];
}

export interface Editing {
  /** Writes `steps` to `collection`; true once written — after the owner's answer, when it was asked. */
  write(collection: string, steps: Step[], what: string): Promise<boolean>;
  /** A record's sheet; from a board, with its columns to move it to. */
  open(collection: string, id: string, move?: Move): void;
  /** The sheet that adds a record by a view's `add`. */
  add(collection: string, spec: AddSpec): void;
  /** Changes written from here so far: what a list of them reads again by. */
  writes: number;
}

const EditContext = createContext<Editing | null>(null);

/** The views' editor, or null where they are read only. */
export function useEditing(): Editing | null {
  return useContext(EditContext);
}

interface Asked {
  collection: string;
  steps: Step[];
  what: string;
  refusal: Refusal;
  settle: (written: boolean) => void;
}

export function Editor({
  api = editApi,
  today,
  refresh,
  reload,
  children,
}: {
  api?: EditApi;
  today: string;
  refresh: (collection: string, rev: string | null) => void;
  reload: () => void;
  children: React.ReactNode;
}) {
  const [asked, setAsked] = useState<Asked | null>(null);
  const [record, setRecord] = useState<{ collection: string; id: string; move?: Move } | null>(null);
  const [adding, setAdding] = useState<{ collection: string; spec: AddSpec } | null>(null);
  const [writes, setWrites] = useState(0);
  const writeRef = useRef<Editing["write"]>(async () => false);

  const write = useCallback(
    async (collection: string, steps: Step[], what: string): Promise<boolean> => {
      try {
        const answer = await api.write(
          collection,
          steps.map((s) => s.op),
          inputId(),
        );
        refresh(collection, answer.rev);
        setWrites((n) => n + 1);
        const undo = undoSteps(answer);
        if (undo.length) toast(what, "info", UNDO_MS, undefined, { label: "Undo", run: () => void writeRef.current(collection, undo, "Undone.") });
        else toast("Already so: nothing changed.");
        return true;
      } catch (e) {
        const refusal = e instanceof ApiError ? refusalOf(e.data) : null;
        if (refusal && (refusal.kind === "stale" || refusal.kind === "after" || refusal.kind === "duplicate")) {
          return new Promise<boolean>((settle) => setAsked({ collection, steps, what, refusal, settle }));
        }
        toast(e instanceof ApiError ? e.message : "Not saved.", "error", 6000);
        return false;
      }
    },
    [api, refresh],
  );
  writeRef.current = write;

  const editing = useMemo<Editing>(
    () => ({
      write,
      open: (collection, id, move) => setRecord({ collection, id, ...(move ? { move } : {}) }),
      add: (collection, spec) => setAdding({ collection, spec }),
      writes,
    }),
    [write, writes],
  );

  const answer = async (again: Step[] | null) => {
    const was = asked;
    setAsked(null);
    if (!was) return;
    if (again === null) {
      reload();
      setWrites((n) => n + 1);
      return was.settle(false);
    }
    was.settle(await write(was.collection, again, was.what));
  };

  return (
    <EditContext.Provider value={editing}>
      {children}
      <Asking asked={asked} onAnswer={(again) => void answer(again)} />
      {record && <RecordSheet {...record} api={api} editing={editing} onClose={() => setRecord(null)} />}
      {adding && <AddSheet {...adding} api={api} today={today} editing={editing} onClose={() => setAdding(null)} />}
    </EditContext.Provider>
  );
}

/** The owner's question about a refused write: the record moved on — apply his change at its new version, or keep
 * what is there; a change taken back that was changed after; a record that may be one already there. */
function Asking({ asked, onAnswer }: { asked: Asked | null; onAnswer: (again: Step[] | null) => void }) {
  const refusal = asked?.refusal;
  const title = refusal?.kind === "duplicate" ? "Already there?" : refusal?.kind === "after" ? "Changed after it" : "Changed meanwhile";
  return (
    <Dialog open={asked !== null} onClose={() => onAnswer(null)} title={title}>
      {asked && refusal && (
        <div className="flex flex-col gap-4" data-testid="conflict">
          {refusal.kind === "duplicate" ? (
            <p className="text-sm">{`It may be the same as “${recordWords(refusal.record?.fields ?? {})}”, which is there already.`}</p>
          ) : (
            <p className="text-sm">{conflictWords(refusal)}</p>
          )}
          {refusal.kind === "after" && <p className="text-sm text-muted-foreground">Undo that change first, or set the value yourself.</p>}
          <div className="flex flex-wrap justify-end gap-2">
            {refusal.kind === "stale" && (
              <>
                <Button variant="ghost" onClick={() => onAnswer(null)}>
                  Keep
                </Button>
                <Button onClick={() => onAnswer(againAt(asked.steps, refusal))}>Apply mine</Button>
              </>
            )}
            {refusal.kind === "after" && <Button onClick={() => onAnswer(null)}>OK</Button>}
            {refusal.kind === "duplicate" && (
              <>
                <Button variant="ghost" onClick={() => onAnswer(null)}>
                  Cancel
                </Button>
                <Button onClick={() => onAnswer(asked.steps.map((s) => ({ ...s, op: { ...s.op, distinct: true } as typeof s.op })))}>Add anyway</Button>
              </>
            )}
          </div>
        </div>
      )}
    </Dialog>
  );
}

// --- a record's sheet ------------------------------------------------------------------------------------------------

const OPS_UNDONE = new Set(["add", "set", "delete", "restore", "revert"]);

function RecordSheet({
  collection,
  id,
  move,
  api,
  editing,
  onClose,
}: {
  collection: string;
  id: string;
  move?: Move;
  api: EditApi;
  editing: Editing;
  onClose: () => void;
}) {
  const [data, setData] = useState<RecordAnswer | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const writes = editing.writes;
  // biome-ignore lint/correctness/useExhaustiveDependencies: a write from here, or a refusal answered, reads it again
  useEffect(() => {
    let gone = false;
    api.record(collection, id).then(
      (answer) => {
        if (!gone) {
          setData(answer);
          setError(null);
        }
      },
      (e) => !gone && setError(e instanceof ApiError ? e.message : "Did not load."),
    );
    return () => {
      gone = true;
    };
  }, [api, collection, id, writes]);

  // The toast names its record, as a drag's does: «Acme: moved to Won.».
  const said = (what: string) => (data?.title ? `${data.title}: ${what}` : what.charAt(0).toUpperCase() + what.slice(1));
  const run = async (steps: Step[], what: string, close = false) => {
    setBusy(true);
    const written = await editing.write(collection, steps, what);
    setBusy(false);
    if (written && close) onClose();
  };

  const record = data?.record;
  const live = record && record.deleted === null;
  const target = record?.id ?? null;
  return (
    <Dialog open sheet onClose={onClose} title={data?.title ?? "Record"}>
      <div className="flex flex-col gap-4 p-4" data-testid="record-sheet">
        {error && <p className="text-sm text-muted-foreground">{error}</p>}
        {!data && !error && <p className="text-sm text-muted-foreground">Loading…</p>}
        {data && record && (
          <>
            {record.deleted !== null && <p className="text-sm font-medium text-destructive">Deleted.</p>}
            {move && live && (
              <fieldset className="flex flex-col gap-1.5">
                <legend className="mb-1 text-xs font-semibold tracking-wide text-muted-foreground uppercase">Move to</legend>
                <div className="flex flex-wrap gap-1.5">
                  {move.values
                    .filter((v): v is string => v !== null)
                    .map((value) => {
                      const here = record.fields[move.field] === value;
                      return (
                        <button
                          key={value}
                          type="button"
                          aria-pressed={here}
                          disabled={busy || here}
                          onClick={() =>
                            void run(
                              [{ op: { op: "set", id: record.id, version: record.version, fields: { [move.field]: value } }, target }],
                              said(`moved to ${valueWords(value)}.`),
                              true,
                            )
                          }
                          className={cn(
                            "min-h-11 rounded-full border px-3 text-sm md:min-h-9",
                            here ? "border-primary bg-primary text-primary-foreground" : "bg-card hover:bg-accent",
                          )}
                        >
                          {valueWords(value)}
                        </button>
                      );
                    })}
                </div>
              </fieldset>
            )}
            <dl className="grid grid-cols-[minmax(6rem,auto)_1fr] gap-x-3 gap-y-1.5 text-sm">
              {Object.entries(data.fields)
                .filter(([name]) => record.fields[name] !== undefined)
                .map(([name, spec]) => (
                  <Field key={name} name={name} spec={spec} value={record.fields[name]} words={data.words} />
                ))}
            </dl>
            <section className="flex flex-col gap-1" aria-label="Changes">
              <h3 className="text-xs font-semibold tracking-wide text-muted-foreground uppercase">Changes</h3>
              <ul className="flex flex-col divide-y rounded-lg border">
                {data.changes.map((change) => (
                  <li key={change.change_id} className="flex items-start gap-2 px-3 py-2">
                    <span className="min-w-0 flex-1">
                      <span className="block text-sm break-words">{changeWords(change, data.fields, data.words).join(" · ")}</span>
                      <span className="block text-xs text-muted-foreground">
                        {[
                          actorWords(change.actor),
                          ago(change.ts),
                          change.as_of && `for ${valueText(change.as_of, { type: "date" })}`,
                          change.foreign.length > 0 && `after reading ${change.foreign.join(", ")}`,
                        ]
                          .filter(Boolean)
                          .join(" · ")}
                      </span>
                    </span>
                    {OPS_UNDONE.has(change.op) && (
                      <Button
                        size="sm"
                        variant="ghost"
                        disabled={busy}
                        aria-label={`Undo: ${changeWords(change, data.fields, data.words).join(", ")}`}
                        onClick={() => void run([{ op: { op: "revert", change_id: change.change_id, version: record.version }, target }], "Undone.")}
                      >
                        Undo
                      </Button>
                    )}
                  </li>
                ))}
              </ul>
            </section>
            <div className="flex justify-end">
              {live ? (
                <Button
                  variant="ghost"
                  className="text-destructive"
                  disabled={busy}
                  onClick={() => void run([{ op: { op: "delete", id: record.id, version: record.version }, target }], said("deleted."), true)}
                >
                  Delete
                </Button>
              ) : (
                <Button variant="outline" disabled={busy} onClick={() => void run([{ op: { op: "restore", id: record.id, version: record.version }, target }], said("restored."))}>
                  Restore
                </Button>
              )}
            </div>
          </>
        )}
      </div>
    </Dialog>
  );
}

function Field({ name, spec, value, words }: { name: string; spec: FieldSpec; value: unknown; words: Record<string, string> }) {
  return (
    <>
      <dt className="text-muted-foreground">{valueWords(name)}</dt>
      <dd dir="auto" className="min-w-0 break-words whitespace-pre-wrap">
        {valueText(value, spec, words)}
      </dd>
    </>
  );
}

// --- adding a record -------------------------------------------------------------------------------------------------

const CONTROL = "flex h-11 w-full min-w-0 rounded-md border border-input bg-transparent px-3 text-base md:h-9 md:text-sm";

function AddSheet({
  collection,
  spec,
  api,
  today,
  editing,
  onClose,
}: {
  collection: string;
  spec: AddSpec;
  api: EditApi;
  today: string;
  editing: Editing;
  onClose: () => void;
}) {
  const [form, setForm] = useState<FormAnswer | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [given, setGiven] = useState<Record<string, string | boolean>>({});
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    let gone = false;
    api.form(collection).then(
      (answer) => {
        if (gone) return;
        setForm(answer);
        setGiven(startValues(spec.fields, answer.fields, spec.values, today));
      },
      (e) => !gone && setError(e instanceof ApiError ? e.message : "Did not load."),
    );
    return () => {
      gone = true;
    };
  }, [api, collection, spec, today]);

  const missing = form ? spec.fields.filter((f) => form.fields[f]?.required && form.fields[f]?.type !== "bool" && !String(given[f] ?? "").trim()) : [];
  const submit = async () => {
    if (!form || missing.length) return;
    setBusy(true);
    const fields = addFields(given, form.fields);
    const written = await editing.write(collection, [{ op: { op: "add", fields }, target: null }], "Added.");
    setBusy(false);
    if (written) onClose();
  };

  return (
    <Dialog open sheet onClose={onClose} title={spec.label}>
      <form
        className="flex flex-col gap-3 p-4"
        data-testid="add-sheet"
        onSubmit={(e) => {
          e.preventDefault();
          void submit();
        }}
      >
        {error && <p className="text-sm text-muted-foreground">{error}</p>}
        {!form && !error && <p className="text-sm text-muted-foreground">Loading…</p>}
        {form &&
          spec.fields.map((name) => {
            const field = form.fields[name];
            if (!field) return null;
            const value = given[name];
            const set = (v: string | boolean) => setGiven((g) => ({ ...g, [name]: v }));
            const label = `${valueWords(name)}${field.required ? "" : " (optional)"}`;
            if (field.type === "bool")
              return (
                <label key={name} className="flex min-h-11 items-center gap-2 text-sm">
                  <input type="checkbox" checked={value === true} onChange={(e) => set(e.target.checked)} className="size-4 accent-primary" />
                  {valueWords(name)}
                </label>
              );
            return (
              <label key={name} className="flex flex-col gap-1">
                <span className="text-sm font-medium">{label}</span>
                {field.type === "enum" || field.type === "ref" ? (
                  <select value={String(value ?? "")} onChange={(e) => set(e.target.value)} className={CONTROL} required={field.required}>
                    <option value="">{field.required ? "Choose…" : "—"}</option>
                    {field.type === "enum"
                      ? (field.values ?? []).map((v) => (
                          <option key={v} value={v}>
                            {valueWords(v)}
                          </option>
                        ))
                      : (form.options[name] ?? []).map((o) => (
                          <option key={o.id} value={o.id}>
                            {o.title}
                          </option>
                        ))}
                  </select>
                ) : (
                  <Input
                    type={field.type === "int" ? "number" : field.type === "date" ? "date" : "text"}
                    inputMode={field.type === "int" ? "numeric" : undefined}
                    step={field.type === "int" ? 1 : undefined}
                    value={String(value ?? "")}
                    onChange={(e) => set(e.target.value)}
                    required={field.required}
                    maxLength={field.type === "text" ? 2000 : undefined}
                  />
                )}
              </label>
            );
          })}
        <div className="flex justify-end gap-2 pt-1">
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button type="submit" disabled={busy || !form || missing.length > 0}>
            {busy ? "Adding…" : "Add"}
          </Button>
        </div>
      </form>
    </Dialog>
  );
}

/** A view's «add» in its section's header. */
export function AddButton({ collection, spec }: { collection: string; spec: AddSpec }) {
  const editing = useEditing();
  if (!editing) return null;
  return (
    <Button size="sm" variant="ghost" onClick={() => editing.add(collection, spec)}>
      <Plus />
      <span>{spec.label}</span>
    </Button>
  );
}
