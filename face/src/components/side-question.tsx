// «Вопрос в сторону» (/btw): a question about a session that does not go into it. The platform starts a session of
// its own on the same track, with an excerpt of this one's last steps that code built; the owner lands in it.
import { useNavigate } from "@tanstack/react-router";
import { useState } from "react";
import { MessageQuestion } from "@/components/icons";
import { Button } from "@/components/ui/button";
import { Dialog } from "@/components/ui/dialog";
import { Textarea } from "@/components/ui/textarea";
import { ApiError, api } from "@/lib/api";
import { toast } from "@/lib/toast";
import { inputId } from "@/lib/utils";

export function SideQuestion({ session, ask = api.side }: { session: string; ask?: typeof api.side }) {
  const [open, setOpen] = useState(false);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [id, setId] = useState(inputId);
  const navigate = useNavigate();

  async function submit() {
    if (!text.trim() || busy) return;
    setBusy(true);
    try {
      const res = await ask(session, text.trim(), id);
      setText("");
      setId(inputId());
      setOpen(false);
      await navigate({ to: "/s/$id", params: { id: res.session } });
    } catch (e) {
      toast(e instanceof ApiError ? e.message : "Вопрос не ушёл.", "error");
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <Button variant="ghost" size="icon" aria-label="Вопрос в сторону" title="Вопрос в сторону" onClick={() => setOpen(true)}>
        <MessageQuestion />
      </Button>
      <Dialog open={open} onClose={() => setOpen(false)} title="Вопрос в сторону">
        <form
          className="flex flex-col gap-3"
          onSubmit={(e) => {
            e.preventDefault();
            void submit();
          }}
        >
          <p className="text-sm text-muted-foreground">
            Ответит отдельная сессия на том же треке: она увидит выдержку из последних шагов этой, но в неё ничего не
            напишет.
          </p>
          <Textarea
            aria-label="Вопрос"
            value={text}
            onChange={(e) => {
              setText(e.target.value);
              setId(inputId());
            }}
            rows={3}
            placeholder="Например: почему он трогает этот файл?"
          />
          <div className="flex justify-end gap-2">
            <Button variant="ghost" onClick={() => setOpen(false)}>
              Отмена
            </Button>
            <Button type="submit" disabled={busy || !text.trim()}>
              {busy ? "Спрашиваю…" : "Спросить"}
            </Button>
          </div>
        </form>
      </Dialog>
    </>
  );
}
