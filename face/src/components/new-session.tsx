// «Новая сессия»: a sphere (its track by default), a track and the first message; the POST carries an input_id, so
// a retry is not a second session.
import { useNavigate } from "@tanstack/react-router";
import { useState } from "react";
import { Button } from "@/components/ui/button";
import { Dialog } from "@/components/ui/dialog";
import { Textarea } from "@/components/ui/textarea";
import { ApiError, api } from "@/lib/api";
import { live } from "@/lib/live";
import { NO_SPHERE, sphereLabel } from "@/lib/spheres";
import { toast } from "@/lib/toast";
import { inputId } from "@/lib/utils";

export function NewSession({ open, onClose, sphere: initial = null }: { open: boolean; onClose: () => void; sphere?: string | null }) {
  const tracks = live.meta?.tracks ?? [];
  const spheres = live.meta?.spheres ?? [];
  const [sphere, setSphere] = useState<string | null>(initial);
  const [track, setTrack] = useState(spheres.find((s) => s.name === initial)?.track ?? "");
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [id, setId] = useState(inputId);
  const navigate = useNavigate();
  const chosen = track || tracks[0]?.name || "";

  async function submit() {
    if (!chosen || !text.trim()) return;
    setBusy(true);
    try {
      const res = await api.newSession(chosen, text.trim(), id, sphere);
      setText("");
      setId(inputId());
      onClose();
      await navigate({ to: "/s/$id", params: { id: res.session } });
    } catch (e) {
      toast(e instanceof ApiError ? e.message : "Сессия не началась.", "error");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog open={open} onClose={onClose} title="Новая сессия">
      <form
        className="flex flex-col gap-3"
        onSubmit={(e) => {
          e.preventDefault();
          void submit();
        }}
      >
        {spheres.length > 0 && (
          <fieldset className="flex flex-col gap-2">
            <legend className="mb-1 text-sm font-medium">Сфера</legend>
            <div className="flex flex-wrap gap-2">
              {[...spheres, null].map((s) => (
                <label
                  key={s?.name ?? ""}
                  className="flex min-h-11 cursor-pointer items-center gap-2 rounded-md border px-3 has-[:checked]:border-primary has-[:checked]:bg-accent"
                >
                  <input
                    type="radio"
                    name="sphere"
                    value={s?.name ?? ""}
                    checked={sphere === (s?.name ?? null)}
                    onChange={() => {
                      setSphere(s?.name ?? null);
                      if (s) setTrack(s.track);
                    }}
                    className="accent-primary"
                  />
                  <span className="text-sm">{s ? sphereLabel(s) : NO_SPHERE}</span>
                </label>
              ))}
            </div>
          </fieldset>
        )}
        <fieldset className="flex flex-col gap-2">
          <legend className="mb-1 text-sm font-medium">Трек</legend>
          <div className="flex flex-wrap gap-2">
            {tracks.map((t) => (
              <label
                key={t.name}
                className="flex min-h-11 cursor-pointer items-center gap-2 rounded-md border px-3 has-[:checked]:border-primary has-[:checked]:bg-accent"
              >
                <input
                  type="radio"
                  name="track"
                  value={t.name}
                  checked={chosen === t.name}
                  onChange={() => setTrack(t.name)}
                  className="accent-primary"
                />
                <span className="text-sm">{t.name}</span>
                <span className="text-xs text-muted-foreground">{t.kind === "code" ? "код" : "личное"}</span>
              </label>
            ))}
            {tracks.length === 0 && <p className="text-sm text-muted-foreground">Треков нет.</p>}
          </div>
        </fieldset>
        <label className="flex flex-col gap-1">
          <span className="text-sm font-medium">Что сделать</span>
          <Textarea value={text} onChange={(e) => setText(e.target.value)} rows={4} placeholder="Например: почини падающий тест в tests/test_gate.py" />
        </label>
        <div className="flex justify-end gap-2">
          <Button variant="ghost" onClick={onClose}>
            Отмена
          </Button>
          <Button type="submit" disabled={busy || !text.trim() || !chosen}>
            {busy ? "Запускаю…" : "Начать"}
          </Button>
        </div>
      </form>
    </Dialog>
  );
}
