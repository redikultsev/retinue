// The face in a real browser against the mock (`pnpm e2e`, docs/face.md «Checks»): its own mock and Vite on ports of
// their own, so a `pnpm dev` already running is never in the way. Chromium only, at a phone's, a tablet's and a
// desktop's width.
import { defineConfig } from "@playwright/test";

const MOCK = 9611;
const WEB = 4173; // an origin the mock lets in (mock/server.ts, ORIGINS)

export default defineConfig({
  testDir: "e2e",
  fullyParallel: true,
  retries: 0,
  reporter: [["list"]],
  use: { baseURL: `http://127.0.0.1:${WEB}`, trace: "retain-on-failure" },
  projects: [
    { name: "phone", use: { browserName: "chromium", viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true } },
    { name: "tablet", use: { browserName: "chromium", viewport: { width: 768, height: 1024 } } },
    { name: "desktop", use: { browserName: "chromium", viewport: { width: 1280, height: 800 } } },
  ],
  webServer: [
    { command: `node mock/server.ts`, env: { MOCK_PORT: String(MOCK) }, port: MOCK, reuseExistingServer: false },
    {
      command: `pnpm exec vite --port ${WEB} --strictPort`,
      env: { RETINUE_API: `http://127.0.0.1:${MOCK}` },
      url: `http://127.0.0.1:${WEB}/`,
      reuseExistingServer: false,
    },
  ],
});
