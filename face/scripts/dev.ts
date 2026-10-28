// `pnpm dev`: the mock face API and Vite together; Vite proxies /api/* to the mock. With RETINUE_API set, only
// Vite runs, proxying to that face API instead.
import { type ChildProcess, spawn } from "node:child_process";

const children: ChildProcess[] = [];
if (!process.env.RETINUE_API) {
  children.push(spawn(process.execPath, ["mock/server.ts"], { stdio: "inherit" }));
}
children.push(spawn("pnpm", ["exec", "vite", ...process.argv.slice(2)], { stdio: "inherit" }));

const stop = () => {
  for (const c of children) c.kill("SIGTERM");
  process.exit(0);
};
process.on("SIGINT", stop);
process.on("SIGTERM", stop);
for (const c of children) c.on("exit", (code) => code && stop());
