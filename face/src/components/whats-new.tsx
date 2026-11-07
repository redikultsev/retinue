// «What's new»: once after an update, what changed since the release the owner last saw, in his words
// (CHANGELOG.md, retinue/changelog.py). «Got it» marks it seen on the platform, so another device does not show it
// again; closing it any other way hides it until the next load.
import { useState } from "react";
import { Button } from "@/components/ui/button";
import { Dialog } from "@/components/ui/dialog";
import { api } from "@/lib/api";
import { live, useLiveState } from "@/lib/live";

export function WhatsNew() {
  useLiveState(); // meta arrives with the first snapshot
  const releases = live.meta?.whats_new ?? [];
  const [closed, setClosed] = useState(false);
  const newest = releases[0];
  if (!newest) return null;
  const gotIt = () => {
    setClosed(true);
    void api.whatsNewSeen(newest.version).catch(() => undefined);
  };
  return (
    <Dialog open={!closed} onClose={() => setClosed(true)} title={`What's new in ${newest.version}`}>
      <div className="flex max-h-[60dvh] flex-col gap-4 overflow-y-auto">
        {releases.map((release) => (
          <section key={release.version} className="flex flex-col gap-1.5">
            {releases.length > 1 && (
              <h3 className="text-sm font-medium text-muted-foreground">
                {release.version}
                {release.date && ` · ${release.date}`}
              </h3>
            )}
            <ul className="flex list-disc flex-col gap-1 pl-5 text-[15px] leading-snug">
              {release.notes.map((note, i) => (
                <li key={i}>{note}</li>
              ))}
            </ul>
          </section>
        ))}
      </div>
      <div className="flex justify-end">
        <Button onClick={gotIt}>Got it</Button>
      </div>
    </Dialog>
  );
}
