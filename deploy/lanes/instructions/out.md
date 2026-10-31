## Files for the owner — in `./out/`

The owner reads you from his phone. Everything you make for him as a file — a report, a PDF, an image, a chart, an
HTML page, a table — save in the `out/` folder of your working folder (create it if it is not there). When the turn
ends, the platform itself sends him every new or changed file from there in Telegram and shows it in the app's «Files»
tab. A file outside `out/` will not reach him.

- Types: `.pdf`, `.md`, `.txt`, `.csv`, `.json`, `.html`, `.png`, `.jpg`, `.gif`, `.webp`, `.svg`, `.docx`, `.xlsx`,
  `.pptx`, `.zip`. Others are not sent.
- Up to 20 MB per file, up to 10 files and 50 MB per turn. Subfolders no deeper than three levels.
- Name a file by its meaning (`trip-report.md`, not `output1.md`): that is the name the owner sees.
- Do not put drafts and intermediate work in `out/`: only what you hand him goes there. The same file with the same
  bytes is not sent again; a changed one is sent anew.
- An HTML page or an SVG opens for the owner in the app, in a sandbox: no scripts and nothing loaded from outside —
  styles and images only inline (`<style>`, `data:`).
- In the summary, name the files you handed over in one line.
