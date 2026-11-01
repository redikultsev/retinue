// A new session of one sphere, from its «＋» in the list: what to do, and nothing else to choose — the sphere is the
// one pressed, and its track is the sphere's own (`platform.yaml`); the session decides how to work.
import { useNavigate } from "@tanstack/react-router";
import { useState } from "react";
import { Button } from "@/components/ui/button";
import { Dialog } from "@/components/ui/dialog";
import { Textarea } from "@/components/ui/textarea";
import { ApiError, api } from "@/lib/api";
import { live } from "@/lib/live";
import { sphereLabel } from "@/lib/spheres";
import { toast } from "@/lib/toast";
import { inputId } from "@/lib/utils";
import { generalTrack, NO_TRACK } from "@/pages/new";

export function NewSession({ open, onClose, sphere: name = null }: { open: boolean; onClose: () => void; sphere?: string | null }) {
  const meta = live.meta;
  const sphere = meta?.spheres?.find((s) => s.name === name) ?? null;
  const track = sphere ? sphere.track : generalTrack(meta);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [id, setId] = useState(inputId);
  const navigate = useNavigate();

  async function submit() {
    if (!text.trim()) return;
    if (!track) {
      toast(NO_TRACK, "error");
      return;
    }
    setBusy(true);
    try {
      const res = await api.newSession(track, text.trim(), id, sphere?.name ?? null);
      setText("");
      setId(inputId());
      onClose();
      await navigate({ to: "/s/$id", params: { id: res.session } });
    } catch (e) {
      toast(e instanceof ApiError ? e.message : "The session did not start.", "error");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog open={open} onClose={onClose} title={sphere ? `New session · ${sphereLabel(sphere)}` : "New session"}>
      <form
        className="flex flex-col gap-3"
        onSubmit={(e) => {
          e.preventDefault();
          void submit();
        }}
      >
        <label className="flex flex-col gap-1">
          <span className="text-sm font-medium">What should I do?</span>
          <Textarea
            autoFocus
            value={text}
            onChange={(e) => setText(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
                e.preventDefault();
                void submit();
              }
            }}
            rows={4}
            placeholder="For example: find a flat in Lisbon for May"
          />
        </label>
        <div className="flex justify-end gap-2">
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button type="submit" disabled={busy || !text.trim()}>
            {busy ? "Starting…" : "Start"}
          </Button>
        </div>
      </form>
    </Dialog>
  );
}
