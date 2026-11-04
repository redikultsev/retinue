# How plugin systems install, update and separate a plugin from the user's data

Research for issue #14 of the wayfinder map (#11). Question: how do established plugin systems describe, install,
update and remove a plugin, keep the user's own data apart from the plugin's files, and treat code that someone else
wrote — and what of that fits a Retinue project?

Read on 2026-11-04. Every claim carries the page it comes from. Where a page is silent, the text says "not documented"
instead of guessing. Retinue's side is read from `docs/base.md`, `docs/instructions.md`, `docs/platform.md` and
`docs/collections.md` of this repository.

Systems read: Claude Code plugins and marketplaces, VS Code extensions, Obsidian community plugins, Home Assistant
custom integrations with HACS. Two more were added because each brings one thing the four lack: Helm charts (how a
user's values stay apart from the package's defaults on upgrade, and signed packages) and git submodules (an exact
pin of someone else's tree inside your own).

## What a Retinue project is, for this comparison

From the map (#11) and the docs:

- A project is a folder of the Base with an `AGENTS.md` (its Showcase), at any depth; projects nest
  ([base.md](base.md), "Reading the Base in the app").
- A repository only delivers a project. The project is the folder in the Base with its manifest.
- Its rules and tools update from the source; the owner's records in it stay his: never overwritten, never sent back.
- Retinue ships no built-in projects.
- A project may bring rules (`AGENTS.md`), skills and tools. Records the owner keeps in tables live in the platform's
  SQLite file, not in the Base ([collections.md](collections.md)).

Two facts of the current design decide how much of the borrowing is possible:

- Commits the platform makes carry no code: `protect` refuses `scripts/**`, `justfile`, `**/*.py`, `**/*.sh`
  ([base.md](base.md), "The tree is hostile input"). A project's tools cannot simply arrive as files of the Base.
- The lanes load skills only as a plugin folder checked against a strict subset (no hooks, no `allowed-tools`, no
  links, an allowed list of frontmatter keys) and re-check it before every run
  ([instructions.md](instructions.md), "Your skills"). Repository rules are read from `AGENTS.md` files inside nonce
  fences and grant nothing ([instructions.md](instructions.md), "The repository's rules").

---

## 1. Claude Code plugins and marketplaces

### Manifest

`.claude-plugin/plugin.json` is optional; without it Claude Code loads what it finds in the standard layout. Only
`name` is required. Fields include `displayName`, `version`, `description`, `author`, `homepage`, `repository`,
`license`, `keywords`, `defaultEnabled`, `dependencies`, `settings`, `userConfig`, and component keys (`skills`,
`commands`, `agents`, `hooks`, `mcpServers`, `lspServers`, `outputStyles`, `workflows`). An unknown top-level key is
stripped with a warning; an unknown key inside `userConfig` and a few other objects is an error and the plugin does not
load. `claude plugin validate` checks it. Source: <https://code.claude.com/docs/en/plugins-reference>.

`userConfig` declares values the user is asked for when the plugin is enabled: `type`, `title`, `description`,
`required`, `default`, `options`, `sensitive`. Non-sensitive values go to `pluginConfigs` in the user's settings;
`sensitive` ones go to the platform's secure credential store. Plugin code reads them as `${user_config.KEY}` or
`CLAUDE_PLUGIN_OPTION_<KEY>`. Source: same page.

A separate catalog file, `marketplace.json`, lists plugins: `name`, `owner`, `plugins[]`; each entry has `name`,
`source`, `description`, `version`, `strict`, `dependencies`, `defaultEnabled`. Source:
<https://code.claude.com/docs/en/plugins/marketplace-reference.md>.

### Fetch from a link, version pinning

Two steps. First the user registers a marketplace: `/plugin marketplace add owner/repo` (GitHub shorthand), any git
URL, a local path, or an `https://` link to a `marketplace.json`; `#ref` pins a branch or tag
(`your-org/plugins#v1.2.0`). Then installs `plugin@marketplace`. In one command:
`claude plugin install deploy-helper --marketplace your-org/plugins`; if the marketplace is new, the session shows the
resolved source and asks for confirmation. Source: <https://code.claude.com/docs/en/discover-plugins> (redirects to
`/plugins/install.md`).

Each entry's `source` is a relative path, or an object: `github` (`repo`, `ref`, `sha`), `url` (any git URL),
`git-subdir` (one subdirectory, sparse checkout), `npm`, `archive` (zip over HTTPS with a `sha256` pin), or `command`.
`sha` is a full 40-character commit and wins over `ref`. Source: marketplace-reference page above.

The installed version is computed: the manifest's `version`, else the entry's `version`, else the commit SHA
(12 characters) of the source. A manifest that pins `"version": "1.0.0"` keeps every user on the cached copy until the
author changes the string, however many commits follow. Source: <https://code.claude.com/docs/en/plugins/loading.md>,
"Versions and updates".

### Update offer and apply

- Each marketplace has an auto-update switch: on by default for Anthropic's official marketplaces, off for every
  other. After the first message of a session, Claude Code waits a random delay of up to ten minutes, refreshes
  marketplaces with auto-update on and updates installed plugins on disk. The running session keeps what it loaded;
  the user sees `Plugin updated: <name> · Run /reload-plugins to apply`.
- Manual: `claude plugin update <plugin>@<marketplace>` or "Update now" in `/plugin`. `claude plugin marketplace update`
  alone refreshes the catalog but leaves installed plugins untouched.
- An update is detected only when the computed version changes.

Source: loading.md and discover-plugins pages above.

### What uninstall leaves

- The plugin's persistent data directory `${CLAUDE_PLUGIN_DATA}` (`~/.claude/plugins/data/<id>/`) is deleted when the
  plugin is uninstalled from its last scope; `--keep-data` keeps it. Source: plugins-reference, "Environment variables".
- The old version directories stay under `cache/` for 14 days (an `.orphaned_at` marker, then a background sweep).
  Source: loading.md, "Cleanup of previous versions".
- Removing a marketplace uninstalls every plugin installed from it and deletes their data, options and secrets.
  Source: marketplace-reference, "Reserved names".
- Dependencies that were installed automatically stay until `claude plugin prune`. Source: discover-plugins.
- `userConfig` values and `enabledPlugins` entries live in the user's settings and are separate from the plugin's
  files. For removal of options, the marketplace removal page says options and secrets go with the plugin; plain
  `uninstall` handling of `pluginConfigs` is not stated on the pages read.

### Where user data lives versus plugin files

Three places, by design:

| What | Where | Survives an update? |
|---|---|---|
| Plugin code | `cache/<marketplace>/<plugin>/<version>/` (`${CLAUDE_PLUGIN_ROOT}`) | No: a new directory per version |
| Plugin's own state | `data/<id>/` (`${CLAUDE_PLUGIN_DATA}`) | Yes |
| The user's choices | settings files: `enabledPlugins`, `pluginConfigs`, secure store for secrets | Yes |

"`${CLAUDE_PLUGIN_ROOT}` changes when the plugin updates, so don't write state there." Source: plugins-reference and
loading.md, "Find plugins on disk". Scopes: user (`~/.claude/settings.json`), project (`.claude/settings.json`,
committed) and local (`.claude/settings.local.json`); more specific wins. A project scope entry enables a plugin for
collaborators but does not download it: each collaborator installs once. Source: discover-plugins, "Choose an install
scope".

### Nesting and dependencies

`dependencies` in `plugin.json`: `"name"`, `"name@marketplace"` or `{name, marketplace, version}`, where `version` is
a semver range resolved against git tags named `<plugin>--v<version>`. Installing a plugin installs its dependencies at
the same scope. Several plugins constraining one dependency resolve to the highest version satisfying all; if the
ranges do not overlap, the install fails. A dependency in another marketplace is installed only if the root
marketplace lists it in `allowCrossMarketplaceDependenciesOn`. A plugin with only `name` and `dependencies` is a valid
bundle. Source: <https://code.claude.com/docs/en/plugins/dependencies.md>.

Components are namespaced under the plugin name (`deploy-tools:reviewer`). A plugin cannot declare components outside
its own directory (a `..` or an outward symlink is rejected). Same-named plugins from different origins follow a fixed
precedence order. Source: plugins-reference, loading.md "Name conflicts".

There is no nesting of plugins inside plugins: a bundle is a flat set of dependencies.

### Trust

- "A Claude Code plugin you install can execute arbitrary code on your machine with your user privileges." Hooks,
  monitors, MCP/LSP servers and `bin/` run outside the sandbox and outside the permission rules. Skills, commands and
  agents enter the model's context as instructions. Source: <https://code.claude.com/docs/en/plugins/security.md>.
- Anthropic "does not control what MCP servers, files, or other software are included in plugins". There is no
  review of third-party marketplaces; the install screen shows a "Will install" list (commands, hooks, servers) and
  the user is told to read `hooks/hooks.json`, `.mcp.json` and `bin/`. Source: security.md.
- Names: official and community marketplace names are reserved and accepted only from `github.com/anthropics/`
  sources; the community catalog pins nearly every plugin to a commit SHA and Claude Code refuses a different commit.
  Source: security.md, marketplace-reference.
- Integrity: `archive` sources can carry a `sha256`; mismatch refuses the install. `npm` sources run no install
  scripts. Source: marketplace-reference, security.md.
- Organisation controls (managed settings): allowlist/blocklist of marketplace sources (`strictKnownMarketplaces`,
  `blockedMarketplaces`), force-enabled plugins, switching off `--plugin-dir`. Source: marketplace-reference.
- A plugin shared through a repository loads only after the folder is trusted; background monitors and some MCP forms
  are dropped for such plugins. Source: loading.md, "Trust check and component limits for repository plugins".
- A `CLAUDE.md` at a plugin's root is not loaded as context; instructions must ride in a skill. Source:
  plugins-reference, "Standard layout".

---

## 2. VS Code extensions

### Manifest

The manifest is the extension's `package.json`. Required: `name` (lowercase, unique), `version` (SemVer),
`publisher`, `engines.vscode` (cannot be `*`). Others: `activationEvents`, `contributes`, `extensionDependencies`
(IDs as `publisher.name`), `extensionPack` (a bundle with no functional dependency), `extensionKind` (`ui` or
`workspace`), `capabilities.untrustedWorkspaces` and `capabilities.virtualWorkspaces`. Source:
<https://code.visualstudio.com/api/references/extension-manifest>.

### Fetch from a link, version pinning

- From the Marketplace: the Install button.
- From a file: "Install from VSIX" or `code --install-extension myextension.vsix`; auto-update is off for VSIX
  installs.
- A specific version: "Install Another Version"; pre-release from the Install button's menu.
- Workspace recommendations: `.vscode/extensions.json` with a `recommendations` list of `publisher.extension` IDs;
  members are prompted once to install them. The file recommends, it does not install.

Source: <https://code.visualstudio.com/docs/configure/extensions/extension-marketplace>. A direct "install from a git
URL" is not documented on that page; the two routes are Marketplace and VSIX.

### Update offer and apply

Auto-update is on by default for enabled extensions, after a delay of 12 hours from publication
(`extensions.autoUpdateDelay`); it can be turned off globally (`extensions.autoUpdate`) or update checks stopped
(`extensions.autoCheckUpdates`). Manual: `@updates` filter, Update, Update All. Disabled extensions update when next
enabled. Source: marketplace page above.

### What uninstall leaves

Uninstall is Manage, then Uninstall, and restart the extension host. Whether an extension's stored data is removed is
not documented on the pages read. The storage guide lists `globalState`, `workspaceState`, `globalStorageUri`,
`storageUri` and `secrets` and says nothing about removal
(<https://code.visualstudio.com/api/extension-capabilities/common-capabilities>). A `vscode:uninstall` script in
`package.json` lets an extension delete its own files; community threads report that global storage can survive an
uninstall and the hook gets no VS Code API (<https://github.com/microsoft/vscode/issues/45474>, a maintainer thread; this
is secondary evidence, not documentation).

### Where user data lives versus plugin files

- Extension code: `~/.vscode/extensions` (`--extensions-dir` changes it).
- Extension state, in the editor's own storage, kept per extension: `workspaceState` (one workspace), `globalState`
  (only keys listed in `setKeysForSync` go through Settings Sync), `storageUri` and `globalStorageUri` (directories for
  large files), `secrets` (encrypted, not synced).
- The user's choices: `settings.json` values declared by the extension under `contributes.configuration`, in the
  user's, workspace's or folder's settings.

Source: marketplace page, common-capabilities page. The extension's folder is therefore replaced on update while the
state and the settings stay in other places.

### Nesting and dependencies

`extensionDependencies` makes other extensions required; `extensionPack` installs a bundle, and the manifest page
says a pack "should not have functional dependencies on its bundled extensions", which stay manageable independently.
No version ranges on dependencies are documented on the page read. Source: extension-manifest page.

### Trust

- "Extensions have the same permissions as VS Code itself." Source: marketplace page and
  <https://code.visualstudio.com/docs/editor/extension-runtime-security>.
- Marketplace: every new extension and update is scanned by several antivirus engines before it is published; a
  clean-room VM tests runtime behaviour; extensions are signed at publication and the signature is verified at
  install (`extensions.verifySignature`); a block list lets VS Code uninstall malicious extensions automatically;
  verified-publisher badge needs domain ownership and six months in good standing. Source: extension-runtime-security.
- Since VS Code 1.97 a first install from a publisher asks whether to trust that publisher; trusting a pack trusts
  its dependencies' publishers; installs from the command line do not trust publishers automatically. Source: same.
- Workspace Trust: a folder opened for the first time is "Restricted Mode"; `capabilities.untrustedWorkspaces` is
  `true`, `false` (default: extension disabled until trusted) or `limited` with `restrictedConfigurations`
  (workspace-defined settings are ignored until trust). Extensions with no `main` entry (themes, grammars) need no
  trust. Source: <https://code.visualstudio.com/api/extension-guides/workspace-trust>.
- Organisations can restrict which extensions are allowed. Source: extension-runtime-security.

---

## 3. Obsidian community plugins

### Manifest

`manifest.json` at the plugin root. Plugin fields: `id` (lowercase letters and hyphens; no "obsidian"; should match the
folder name), `name`, `version` (x.y.z), `minAppVersion`, `description`, `author`, `isDesktopOnly` (all required);
`authorUrl`, `fundingUrl` optional. Source: <https://docs.obsidian.md/Reference/Manifest>. A `versions.json` at the
repository root maps plugin versions to the `minAppVersion` they need; an older app gets the newest compatible
version. Source: <https://docs.obsidian.md/Reference/Versions>.

### Fetch from a link, version pinning

- Normal route: Settings, Community plugins, Browse, Install. Restricted Mode must be off first. Installed plugins
  stay inactive until enabled. Source: <https://obsidian.md/help/community-plugins>.
- The directory is a list of `id`, `name`, `author`, `description`, `repo` (`owner/repo`) in
  `obsidianmd/obsidian-releases/community-plugins.json`
  (<https://raw.githubusercontent.com/obsidianmd/obsidian-releases/master/community-plugins.json>). The directory
  reads `manifest.json` from the default branch's head, but the files are downloaded from a GitHub release whose tag
  equals the manifest `version` exactly; the release must attach `main.js`, `manifest.json` and optionally
  `styles.css`. Source: <https://docs.obsidian.md/Plugins/Releasing/Submit+your+plugin>.
- Install from a link without the directory (a plugin by a repository URL): not offered by the app; the app's help
  does not describe manual installation. Copying the three files into the vault's plugins folder is the manual route,
  which the help page read does not document.

### Update offer and apply

"Plugins don't update automatically, for security reasons." The user selects "Check for updates", then updates all
or one. Source: <https://obsidian.md/help/community-plugins>. Obsidian fetches the files from the GitHub release whose
tag matches the manifest version (submit page above).

### What uninstall leaves

The help page does not say what happens to saved settings. The plugin's API says its data "is stored in `data.json` in
the plugin folder" (`loadData`/`saveData` in
<https://github.com/obsidianmd/obsidian-api/blob/master/obsidian.d.ts>). Because the settings sit inside the plugin's
own folder, an uninstall that removes the folder removes them; one community guide advises backing up
`.obsidian/plugins/<id>/data.json` before removing a plugin
(<https://tasknotes.dev/guides/backup-recovery/>, third-party). Treat "settings go with the folder" as likely, not
confirmed by Obsidian's documentation.

### Where user data lives versus plugin files

Plugin files and the plugin's settings share one folder, `.obsidian/plugins/<id>/` (`main.js`, `manifest.json`,
`styles.css`, `data.json`). The user's notes are the rest of the vault; plugins read and write them through the vault
API (guidelines: prefer `app.vault` and `Vault.process` for atomic edits;
<https://docs.obsidian.md/Plugins/Releasing/Plugin+guidelines>). A plugin update replaces `main.js`, `manifest.json`
and `styles.css`; `data.json` is the plugin's own file and is not part of the release. So here code and settings are
beside each other but only the settings are written by the plugin at runtime.

### Nesting and dependencies

None documented. A plugin has no dependency field, and the manifest has no way to name another plugin.

### Trust

- Restricted Mode is on by default and blocks third-party code. "Obsidian cannot reliably limit plugins to specific
  permissions or access levels": plugins can read files on the computer, connect to the internet and install
  programs. Source: <https://obsidian.md/help/Extending+Obsidian/Plugin+security>.
- Review: every plugin version is scanned automatically for vulnerabilities, code quality and malware, with a
  scorecard on the directory page; manual review is only for popular, featured and flagged plugins; the page says the
  scans "cannot guarantee that a plugin is safe". Source: same.
- A submission needs a repository with README, LICENSE and `manifest.json`, a verified GitHub owner and an automated
  review pass. Source: submit page above.

---

## 4. Home Assistant custom integrations and HACS

### Manifest

`custom_components/<domain>/manifest.json`. Required keys: `domain` (lowercase letters, digits, underscores; must
equal the folder name), `name`, `codeowners`, `documentation` (https), `iot_class` for integrations that connect to
something; `version` is required for custom integrations only (SemVer or CalVer). Others: `requirements` (pip packages
"pinned to an exact version"; the integration fails to load if installing them fails), `dependencies` (integrations
that load first), `after_dependencies`, `config_flow`, `integration_type`, `single_config_entry`, `issue_tracker`,
`disabled`, and discovery keys. Source: <https://contributors.home-assistant.io/core/integrations/manifest/>.

HACS adds `hacs.json` at the repository root: `name` (required), `content_in_root`, `zip_release` with `filename`,
`hide_default_branch`, `country`, `homeassistant` (minimum version), `hacs` (minimum), and **`persistent_directory`**
("Relative path inside the integration directory that is preserved during upgrades"). One integration per repository.
Source: <https://www.hacs.xyz/docs/publish/start/> and <https://www.hacs.xyz/docs/publish/integration/>. The HACS
integration page also lists `domain`, `documentation`, `issue_tracker`, `codeowners`, `name`, `version` as required
manifest keys.

### Fetch from a link, version pinning

HACS menu, "Custom repositories", enter the repository URL, pick a category (Integration, Dashboard, Theme, Template,
Python Script, AppDaemon), Add. Only public GitHub repositories work. The repository needs a recognised structure,
and "not all repositories will work". Source: <https://www.hacs.xyz/docs/faq/custom_repositories/> and the publishing
rules page.

Versions: with GitHub releases, HACS takes the latest release tag as the remote version ("just publishing tags is not
enough"); without releases, it uses the first seven characters of the last commit. The user can download "a different
version" from the entry's menu (HACS shows the five latest releases plus the default branch). Source:
<https://www.hacs.xyz/docs/publish/start/>, <https://www.hacs.xyz/docs/publish/integration/>,
<https://www.hacs.xyz/docs/use/repositories/dashboard/>.

### Update offer and apply

HACS checks repositories regularly; a repository shows "Pending update". "Update information does not change the
downloaded repository content" — the user must press Redownload. After a download the status is "Pending restart" and
the new version is active only after Home Assistant restarts. Source: dashboard page above.

### What uninstall leaves

HACS "Remove" deletes the repository's files from the configuration directory. "Removing it does not remove related
data. Check the repository's documentation for cleanup instructions." Source: dashboard page. The config entries the
user created for the integration are a separate store (below); removing an entry runs the integration's
`async_remove_entry` and Home Assistant itself removes the entry's devices, entities and repair issues. Source:
<https://contributors.home-assistant.io/core/integrations/config-entries/>.

### Where user data lives versus plugin files

- Code: `custom_components/<domain>/`, replaced on update, except the `persistent_directory` named in `hacs.json`.
- The user's setup: **config entries**, one per configured instance. Each has `data` (connection settings that
  normally do not change, such as a host or token), `options` (what the user adjusts later), `unique_id`, `version` and
  `minor_version`. Home Assistant stores them and sets them up again at every start. The integration is code that
  reads entries; it does not own them. Source: config-entries page. (The page does not name the storage file.)
- Entry versions are the migration contract: if the stored version is older, `async_migrate_entry` runs before setup;
  an entry with a newer major version than the code fails to set up without calling the code. Source: same.

This is the clearest separation of the four: code is replaced wholesale; the user's choices sit in a store the
integration reads and migrates, with a version number on the data.

### Nesting and dependencies

`dependencies` and `after_dependencies` name other integrations; `requirements` pins Python packages. Dependencies
are other integrations of the same system, not versioned plugin ranges. A custom integration whose domain matches a
core integration is not accepted into the HACS default list. Source: manifest page, include page.

### Trust

- HACS default list: the repository must pass the HACS Action and (for integrations) hassfest with no ignores, have a
  full GitHub release, brand assets, an OSI-approved licence, enabled issues, topics, and be submitted by the owner or
  a major contributor; maintainers review the pull request and "reviews take months because of a backlog". Source:
  <https://www.hacs.xyz/docs/publish/include/>.
- Custom repositories skip all of this; the HACS page gives no trust guidance for them. Source: custom repositories
  FAQ.
- A custom integration is Python in the Home Assistant process; no permission model is documented on the pages read.

---

## 5. Two extras

### Helm charts: the user's values stay apart from the chart

A chart is a versioned package (`Chart.yaml`: `apiVersion`, `name`, `version`, `appVersion`, `dependencies` with
`repository`). `values.yaml` holds defaults; the user's files and `--set` flags are merged over them, so only the
overridden keys change. Source: <https://helm.sh/docs/topics/charts/>.

On upgrade the user's values are handled by flag: `--reuse-values` starts from the previous release's values;
`--reset-values` goes back to the chart's defaults; `--reset-then-reuse-values` applies defaults, then the previous
values, then new overrides. `--version` pins an exact tag or range; without it the latest is used. Source:
<https://helm.sh/docs/helm/helm_upgrade/>.

Signing: `helm package --sign` writes a `.prov` file holding `Chart.yaml` and a SHA-256 of the package, signed with
OpenPGP; `helm install --verify` aborts before rendering when verification fails. Source:
<https://helm.sh/docs/topics/provenance/> (the page notes it is not yet updated for Helm 4).

What it brings that the four lack: the user's values are a file of their own, merged over defaults, and the upgrade
command says explicitly which side wins.

### Git submodules: an exact pin of someone else's tree

A superproject records a submodule as a commit SHA, not a branch; `git submodule update` checks out exactly that
commit; `--remote` moves to the upstream tip, and nothing changes in the superproject until the new pointer is
committed. A submodule is checked out as a detached HEAD, so local commits there can be lost on the next update unless
a branch is used or `--merge`/`--rebase` is chosen; Git refuses to overwrite uncommitted work. Source:
<https://git-scm.com/book/en/v2/Git-Tools-Submodules>.

What it brings: the pin is a property of the consumer's tree, and "update" is an explicit commit of the consumer. Its
weakness is the one Retinue's rule names: the owner's edits inside a delivered tree collide with updates. (Claude Code
itself handles marketplaces that use submodules by fetching only those that hold plugin files:
<https://code.claude.com/docs/en/plugins/create-marketplace.md>.)

---

## Comparison

"Not read" in the Helm column means the pages read do not say.

| | Claude Code | VS Code | Obsidian | Home Assistant + HACS | Helm |
|---|---|---|---|---|---|
| Manifest | `plugin.json` (only `name` required), optional | `package.json` (`name`, `version`, `publisher`, `engines`) | `manifest.json` (9 required) | `manifest.json` (+ `hacs.json`) | `Chart.yaml` |
| Catalog | `marketplace.json` in any git repo | Marketplace | `community-plugins.json` in one repo | HACS default list, or custom URL | Chart repository |
| From a link | marketplace by `owner/repo`, git URL, file, https; one-step `--marketplace` | VSIX file; no git-URL install documented | Not by link; only the directory | Custom repository by GitHub URL | Not read |
| Pin | `#ref` on the marketplace; `ref` and 40-char `sha` per plugin; `sha256` for archives | Install another version | Release tag = manifest version | Release tag, or last commit (7 chars); choose a version in the UI | `--version` (exact or range) |
| Computed version | manifest, entry, or commit SHA | manifest `version` | manifest `version` | release tag or commit | `version` in `Chart.yaml` |
| Update offered | Auto-update per marketplace (off by default except official) | Auto, 12 h delay, on by default | Manual only, "for security reasons" | "Pending update", then Redownload | Not read |
| Update applied | Next session or `/reload-plugins` | Restart extension host | In app | After restart of Home Assistant | `helm upgrade` |
| Code location | Versioned dir in `cache/` | `~/.vscode/extensions` | `.obsidian/plugins/<id>/` | `custom_components/<domain>/` | Not read |
| User choices | Settings files (`enabledPlugins`, `pluginConfigs`), secret store | `settings.json`; editor-managed state | `data.json` in the plugin's folder | Config entries (versioned, migrated) | User values file merged over defaults |
| Plugin's own state | `${CLAUDE_PLUGIN_DATA}`, survives updates | `globalStorageUri`, `storageUri`, `globalState` | Same folder as code | `persistent_directory` (HACS) | Not read |
| Uninstall leaves | Data deleted unless `--keep-data`; old versions 14 days | Not documented; reports of leftovers | Not documented; likely settings go | Files removed; "does not remove related data" | Not read |
| Dependencies | Yes, with semver ranges on git tags; cross-marketplace allowlist | `extensionDependencies`, `extensionPack` | None | `dependencies`, `requirements` (exact pins) | `dependencies` + condition/tags |
| Nesting | Bundles only (flat) | Packs only (flat) | None | None | Subcharts, values nest under the subchart's name |
| Review | None for third parties; official names reserved | Malware scan, signing, verified publisher | Automated scan every version, manual only for popular | HACS Action + hassfest, maintainer PR review (custom: none) | Optional PGP signature |
| Permissions | None; hooks and servers run with user rights | None; Workspace Trust gates | None; Restricted Mode is all-or-nothing | None documented | Not read |
| Trust gate for a shared repo | Folder trust before a project plugin loads | Workspace Trust, Restricted Mode | Restricted Mode | — | `--verify` |

---

## What a Retinue project could borrow

Each point names its source and says what in Retinue it answers. Numbers in brackets point to the "Not yet specified"
list on the map.

1. **Keep the project's package and the owner's data in separate places, and name the place for the package's own
   state.** Claude Code gives a plugin three places: versioned code, a persistent data directory that survives updates,
   and settings in the user's files; its docs say not to write state into the versioned directory. HACS adds a single
   `persistent_directory` that survives an upgrade. A Retinue project folder holds both the delivered rules and the
   owner's records in one folder, so the manifest should say which paths are the package's (replaced on update) and
   which the owner's (never touched). A short list of paths in the manifest, like `persistent_directory`, is the
   smallest form. Sources: plugins-reference, loading.md "Find plugins on disk", HACS publish/start. [manifest fields;
   package/data split]

2. **Put anything the owner chooses in a versioned store the package reads, not in the package.** Home Assistant's
   config entries (`data`, `options`, a version, a migration hook) are what stays when the integration is replaced; an
   entry with a newer major version than the code refuses to load instead of being misread. Helm does the same with a
   values file merged over defaults, and its upgrade flags say which side wins. For Retinue this answers where a
   project's settings and tokens go (the Settings screen) and how a new package version meets old settings: store the
   settings with the package version they were written for, and run a migration when the manifest asks. Sources:
   config-entries page; helm upgrade and charts pages. [settings; secrets reaching the container]

3. **Declare the settings a project asks for in the manifest, and mark secrets.** Claude Code's `userConfig` lists
   fields with `type`, `title`, `description`, `required`, `default`, `options` and `sensitive`; the app draws the
   dialog and routes sensitive values to a secret store. A Retinue project's manifest could carry the same shape so the
   wizard and Settings are drawn from it and no install script asks. Source: plugins-reference "User configuration".
   [Settings screen; wizard steps]

4. **Fetch from one link with a pin, and show what will arrive before it is attached.** Claude Code's one-step
   `install <name> --marketplace owner/repo` shows the resolved source and asks for confirmation; `#ref` pins a tag;
   a `sha` pins a commit and wins over `ref`; a "Will install" list names commands, hooks and servers before the
   choice of scope. For «＋» with a pasted link: resolve the link, show the manifest and the list of what the project
   brings (rules, skills, tools, settings, collections), then attach. Pin by commit; keep the tag the owner pasted for
   display. Sources: discover-plugins, marketplace-reference "Plugin sources", security.md "Review a plugin before you
   install". [adding and removing a project]

5. **Treat an update as an offer, applied on the owner's word, with the version computed from the source.** Obsidian
   updates only by hand "for security reasons"; HACS shows "Pending update" and only Redownload applies it; Claude Code
   leaves auto-update off for every third-party marketplace and computes the version from the commit SHA when the
   manifest names none. A project's manifest version, else the commit, decides whether an update exists; the owner
   sees it and presses apply. A pin in the manifest (`version`) keeps users on a version until the author changes it
   (Claude Code's own warning about that is worth copying into the author's docs). Sources: obsidian.md/help
   community-plugins; hacs.xyz dashboard; loading.md "Versions and updates". [adding and removing a project]

6. **Make detaching explicit about what it keeps, with a choice.** Claude Code deletes the plugin's data on
   uninstall unless `--keep-data`; HACS removes the files and says it does not remove related data; Obsidian and VS
   Code do not document it, and users report leftovers. For Retinue the map already says the owner's records stay his,
   so the default should be "package removed, records kept", with the detach screen listing what will stay (paths and
   counts) and an explicit second action to delete them. Sources: plugins-reference "Environment variables";
   hacs.xyz dashboard; VS Code issue thread. [adding and removing a project]

7. **Keep old package versions for a short time after an update or removal.** Claude Code leaves the previous version
   directory for 14 days so that a running session keeps working. Retinue sessions run while the Base changes under
   them (the platform commits after every turn), so a replaced package should not vanish under a running turn.
   Source: loading.md "Cleanup of previous versions". [adding and removing a project]

8. **Nesting: name children by path, use a flat dependency list for requirements, and allow only same-source
   dependencies by default.** Nobody read has real plugin-in-plugin nesting. Claude Code namespaces every component
   under the plugin name (`plugin:skill`) and lets a plugin depend on others by name with semver ranges on git tags
   (`<plugin>--v<version>`); a dependency from another marketplace is installed only if the root marketplace lists it
   (`allowCrossMarketplaceDependenciesOn`). Helm nests subcharts and addresses their values under the subchart's name.
   In Retinue the folder tree already nests projects and gives every file to the nearest project above it, so nesting
   needs no new mechanism for placement; what the manifest needs is a plain `requires` list (project and version
   range) and a rule that a project may name only projects from its own source unless the owner has attached the
   other one. Sources: dependencies.md; marketplace-reference; helm charts page; base.md "A project". [manifest
   fields; nesting]

9. **Use the package's own name as a namespace for its skills, and keep the lane's strict skills check.** Claude Code
   prefixes every component with the plugin name (`deploy-tools:reviewer`); two skills of one name cannot clash. The
   lanes today prefix skills by the plugin they come from (`acme-tdd`) for the same reason and check them against a
   subset with no hooks. A project's skills could be exposed as `<project>:<skill>` and run through the same
   `check_skills` the lanes use before every run. Sources: plugins-reference "name"; instructions.md "Installing them".
   [a project's own skills and tools]

10. **Load a project's rules only inside the project's folder, as text that grants nothing.** Claude Code does not
    load a plugin's root `CLAUDE.md` as context and says instructions must ride in a skill; Retinue already reads
    `AGENTS.md` from the root down to the working directory, fenced, granting nothing. A delivered project's
    `AGENTS.md` is therefore safe to read in a session started in that project's folder; keep it out of sessions
    started elsewhere unless the owner attached the project. Sources: plugins-reference "Standard layout";
    instructions.md "The repository's rules". [trust; skills and tools]

11. **Separate "text and instructions" from "code that runs", and give them different gates.** Claude Code's security
    page splits what enters the model's context (skills, commands, agents) from what runs by itself (hooks, monitors,
    servers, `bin/`), and says permission rules and the sandbox cover only the model's tool calls. Retinue's design is
    the same split: the Base takes no code (`protect`), skills pass the subset check. A project that brings tools needs
    a path for code of its own that is not the Base's commit path. This is a constraint more than a borrowing: HACS
    and Claude Code both ship code in the same package as text, and Retinue's docs forbid that path for the Base.
    Sources: security.md "Understand what a plugin can do"; base.md "The tree is hostile input"; instructions.md.
    [trust; skills and tools]

12. **Show a trust screen before attaching, listing capabilities from the manifest, and make it a gate each time the
    source or the pinned commit changes.** Claude Code shows a warning that Anthropic does not control plugin
    contents and a "Will install" list, refuses a different commit where a catalog pins one, and puts managed
    allow/block lists on sources. VS Code asks once per publisher (and trusting a pack trusts its dependencies'
    publishers). Obsidian's safety scorecard is automated and says it cannot guarantee safety. None of the four has a
    real permission model for third-party code; the honest message is "this folder will be read as instructions by a
    model with your tools". Retinue can do better than the four only by what its lanes already enforce: the checks on
    skills, fenced rules, the classifier. Show those as the limit, and show what a shared project is not covered by.
    Sources: security.md; extension-runtime-security; obsidian.md Plugin security. [trust]

13. **Declare capabilities and let the app withhold what is not trusted, VS Code style.** `capabilities.untrustedWorkspaces`
    (`true`/`false`/`limited` with `restrictedConfigurations`) lets an extension say which of its features need trust,
    and the editor withholds workspace-defined values for the restricted ones. A project manifest could list what it
    uses (skills, tools, collections, a Telegram command) so that a freshly attached project starts with the
    text-only part and the owner switches on the rest. Source: workspace-trust guide. [trust]

14. **Pin the delivered tree by commit and keep the owner's records outside the delivered tree's update path.** Git
    submodules record a commit, update only when the consumer commits the new pointer, and lose local commits made on
    a detached HEAD. That is the failure the map rules out. The safer shape, taken from Claude Code (code copied into a
    per-version cache) and HACS (`persistent_directory`), is to copy the package part into the project folder by
    path list and leave everything else alone. Sources: git-scm submodules chapter; plugins-reference;
    hacs.xyz publish/start. [package/data split; adding and removing a project]

15. **A project in the same folder as the owner's records needs a path list, not a repository, as the unit of update.**
    Obsidian is the cautionary case: code and `data.json` share one folder and rely on the plugin's discipline;
    Claude Code and Home Assistant keep them apart structurally. A manifest in the project folder with `package:
    [AGENTS.md, skills/, ...]` and `data: [records/, ...]` lets update replace exactly the first list. Sources:
    obsidian-api `loadData`; plugins-reference. [package/data split]

16. **A catalog is optional; a link is enough.** Claude Code accepts a bare git repository as a marketplace and HACS
    accepts a bare GitHub URL as a custom repository; both keep a curated default list apart, with review. Retinue
    ships no built-in projects, so only the link route is needed at first. If a curated list comes later, the HACS
    default-list checklist (validation action passes, a full release, a licence, an owner submitting) is a ready
    model. Sources: discover-plugins; hacs.xyz faq custom repositories; hacs.xyz include. [adding and removing a
    project]

17. **Add a validate command early.** `claude plugin validate` and HACS's validation action are what keep a manifest
    typo from loading silently. A Retinue `manifest` check run on attach (and by the author before publishing) would
    do the same. Source: plugins-reference "Validate the manifest"; hacs.xyz include. [manifest fields]

### What not to borrow

- Auto-update by default for third-party sources: only Anthropic's own marketplaces default to it; Obsidian rejects it
  outright. Source: discover-plugins; obsidian.md help.
- Hooks that run commands outside the sandbox, shipped inside a content package: both Claude Code and Retinue's lane
  check treat them as the dangerous part. Source: security.md; instructions.md.
- Settings kept in the plugin's own folder (Obsidian's `data.json`): the update then depends on the plugin not
  clobbering it. Source: obsidian-api.
- A name check by reserved words alone: Claude Code reserves official names and checks impersonation, but that protects
  the brand, not the user from a third-party project. Source: marketplace-reference "Reserved names".

### Not found in the sources read

- A documented permission model for third-party code in any of the four systems: all run with the host's rights.
- Obsidian's and VS Code's behaviour on uninstall for stored data.
- Nesting of plugins inside plugins; only flat dependencies and bundles, plus Helm's subcharts.
- Any system where the package ships inside the user's own data folder and is updated in place while the user's
  records sit beside it (closest: Obsidian's plugin folder; HACS's `persistent_directory`).

## Sources

Claude Code:
- <https://code.claude.com/docs/en/plugins-reference>
- <https://code.claude.com/docs/en/plugin-marketplaces> and <https://code.claude.com/docs/en/plugins/create-marketplace.md>
- <https://code.claude.com/docs/en/plugins/marketplace-reference.md>
- <https://code.claude.com/docs/en/discover-plugins> (install, scopes, updates)
- <https://code.claude.com/docs/en/plugins/loading.md>
- <https://code.claude.com/docs/en/plugins/dependencies.md>
- <https://code.claude.com/docs/en/plugins/security.md>
- <https://code.claude.com/docs/en/settings>

VS Code:
- <https://code.visualstudio.com/api/references/extension-manifest>
- <https://code.visualstudio.com/docs/configure/extensions/extension-marketplace>
- <https://code.visualstudio.com/api/extension-guides/workspace-trust>
- <https://code.visualstudio.com/api/extension-capabilities/common-capabilities>
- <https://code.visualstudio.com/docs/editor/extension-runtime-security>
- <https://github.com/microsoft/vscode/issues/45474> (secondary)

Obsidian:
- <https://docs.obsidian.md/Reference/Manifest>, <https://docs.obsidian.md/Reference/Versions>
- <https://docs.obsidian.md/Plugins/Releasing/Submit+your+plugin>
- <https://docs.obsidian.md/Plugins/Releasing/Plugin+guidelines>
- <https://docs.obsidian.md/Plugins/User+interface/Settings>
- <https://obsidian.md/help/community-plugins>, <https://obsidian.md/help/Extending+Obsidian/Plugin+security>
- <https://github.com/obsidianmd/obsidian-api/blob/master/obsidian.d.ts>
- <https://raw.githubusercontent.com/obsidianmd/obsidian-releases/master/community-plugins.json>
- <https://tasknotes.dev/guides/backup-recovery/> (secondary)

Home Assistant and HACS:
- <https://contributors.home-assistant.io/core/integrations/manifest/>
- <https://contributors.home-assistant.io/core/integrations/config-entries/>
- <https://www.hacs.xyz/docs/publish/start/>, <https://www.hacs.xyz/docs/publish/integration/>,
  <https://www.hacs.xyz/docs/publish/include/>
- <https://www.hacs.xyz/docs/use/repositories/dashboard/>, <https://www.hacs.xyz/docs/faq/custom_repositories/>

Extras:
- <https://helm.sh/docs/topics/charts/>, <https://helm.sh/docs/helm/helm_upgrade/>, <https://helm.sh/docs/topics/provenance/>
- <https://git-scm.com/book/en/v2/Git-Tools-Submodules>
