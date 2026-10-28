// `/f/<file id>`: one file a session handed over — what a Telegram message's «Открыть» opens for an HTML page or an
// SVG, which never go to Telegram as themselves (they could run there). The row comes from
// `GET /api/files/{id}/meta`; a page opens at once in the sandboxed frame of «Файлы».
import { Link } from "@tanstack/react-router";
import { useEffect, useState } from "react";
import { FileRow } from "@/components/files";
import { ArrowLeft } from "@/components/icons";
import { ApiError, api } from "@/lib/api";
import type { OutFile } from "@/lib/types";

export function FilePage({ id, load = api.file }: { id: string; load?: typeof api.file }) {
  const [file, setFile] = useState<OutFile | null>(null);
  const [missing, setMissing] = useState<string | null>(null);

  useEffect(() => {
    let gone = false;
    load(id)
      .then(({ file: found }) => {
        if (!gone) setFile(found);
      })
      .catch((e) => {
        if (!gone) setMissing(e instanceof ApiError && e.status === 404 ? "Такого файла нет: файлы хранятся 30 дней." : "Файл не загрузился.");
      });
    return () => {
      gone = true;
    };
  }, [id, load]);

  return (
    <div className="flex min-h-dvh flex-col">
      <header className="flex min-h-14 items-center gap-2 border-b px-2 pt-[env(safe-area-inset-top)]">
        <Link to="/" aria-label="К списку" className="inline-flex size-11 items-center justify-center rounded-md hover:bg-accent">
          <ArrowLeft className="size-5" />
        </Link>
        <span className="font-semibold">Файл</span>
      </header>
      <main className="mx-auto flex w-full max-w-3xl flex-col gap-3 p-3 pb-[max(1rem,env(safe-area-inset-bottom))]">
        {file ? (
          <>
            <ul>
              <FileRow file={file} opened />
            </ul>
            <Link to="/s/$id" params={{ id: file.session }} search={{ tab: "files" }} className="text-sm text-primary underline">
              Открыть сессию
            </Link>
          </>
        ) : (
          <p className="py-8 text-center text-sm text-muted-foreground">{missing ?? "Загружаю файл…"}</p>
        )}
      </main>
    </div>
  );
}
