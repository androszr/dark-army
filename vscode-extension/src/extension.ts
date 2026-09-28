// Dark Army IDE Bridge.
//
// One instance activates per VS Code window (onStartupFinished). It runs a
// loopback HTTP server and writes ~/.dark-army/ide/<port>.lock so the Dark
// Army daemon can discover every window's server. The daemon fans out a
// `reveal_terminal` request to all of them; the window that owns the terminal
// running a given Claude PID self-selects (ancestor-walk of Terminal.processId,
// tty fallback) and focuses that tab.
//
// The window RAISE is done by the daemon (a CLI `code <path>` invocation): an
// extension cannot raise its own OS window from the background — macOS blocks
// background self-focus. This extension therefore only focuses the terminal TAB;
// it returns its window IDENTITY so the daemon knows which window to raise.
//
// "Identity" is `workspaceFile`, not the folder list, and the difference is the
// whole reason reveals used to land on the wrong window. `code <path>` reuses an
// existing window only when the path is what that window was *opened as*. A
// window opened as a workspace — saved `.code-workspace` or untitled — is
// identified by that workspace, and asking `code` for one of the folders inside
// it matches nothing and opens a SECOND window. A single-folder workspace is
// indistinguishable from a folder window by `workspaceFolders` alone, so the
// folder list cannot be used to decide; `workspaceFile` is what disambiguates.

import * as vscode from 'vscode';
import * as http from 'http';
import * as fs from 'fs';
import * as os from 'os';
import * as path from 'path';
import { execFile } from 'child_process';
import { randomUUID } from 'crypto';

const EXT_VERSION = '0.1.22';
const EXTENSION_ID = 'dark-army.dark-army-ide';

// Workspace Trust. The manifest declares `untrustedWorkspaces: limited`, so
// this bridge also activates in a Restricted Mode window: it binds
// 127.0.0.1:0 with a random token, writes its 0600 lock, and can find, show
// and close this window's terminals — none of which runs anything from the
// workspace. The two things that would are refused until the person trusts
// the folder: starting an assistant here (`spawn_agent`, whose own config
// lives in the workspace) and typing into a terminal here (`send_text`,
// `reply_native_terminal`). Read at the moment of each op, never cached:
// trust can be granted mid-session. `isTrusted` is true wherever trust is
// switched off machine-wide. The refusal is a reply, never a throw — the
// daemon puts a reply's `error` on the card verbatim, and a thrown error
// would read as "no VS Code window could start it".
const UNTRUSTED = 'this folder is not trusted in VS Code — choose Trust in the Workspace Trust banner, then try again';

function trusted(): boolean { return vscode.workspace.isTrusted; }

// Dark Army's state folder: `~/.dark-army` where it exists, else the
// `~/.bob-companion` of a Mac still on the old install, else `~/.dark-army`.
// Decided when used, never at load: the app renames the old folder in place on
// its first launch (the old name then links to the new one), and this bridge
// must not create the new folder before that move has happened.
function stateHome(): string {
  const fresh = path.join(os.homedir(), '.dark-army');
  if (isDir(fresh)) { return fresh; }
  const legacy = path.join(os.homedir(), '.bob-companion');
  if (isDir(legacy)) { return legacy; }
  return fresh;
}

function isDir(p: string): boolean {
  try { return fs.statSync(p).isDirectory(); } catch { return false; }
}

function ideDir(): string { return path.join(stateHome(), 'ide'); }
// The daemon's own loopback API — the one place this extension *calls* Dark Army
// rather than answering it. Hardcoded to `api_server.API_PORT`'s default: the
// extension host does not inherit the daemon's environment, so a machine that
// overrode BOB_COMPANION_API_PORT gets the honest "not reachable" line rather
// than a hang.
const BOB_API_HOST = '127.0.0.1';
const BOB_API_PORT = 19874;
function tokenPath(): string { return path.join(stateHome(), 'api-token'); }

let server: http.Server | undefined;
let lockPath: string | undefined;
let out: vscode.OutputChannel;

export function activate(context: vscode.ExtensionContext) {
  out = vscode.window.createOutputChannel('Dark Army IDE');
  context.subscriptions.push(out);

  const authToken = randomUUID();
  server = http.createServer((req, res) => handleRequest(req, res, authToken));

  server.on('error', (err) => {
    out.appendLine(`server error: ${err}`);
  });

  // 127.0.0.1:0 → ephemeral port; the port is this window's discriminator.
  server.listen(0, '127.0.0.1', () => {
    const addr = server!.address();
    if (!addr || typeof addr === 'string') {
      out.appendLine('failed to obtain server port');
      return;
    }
    const port = addr.port;
    writeLock(port, authToken);
    out.appendLine(`listening on 127.0.0.1:${port}, lock ${lockPath}`);
  });

  // The reverse of Jump: bring Dark Army's panel forward on whatever session this
  // window's active terminal is running. The command is the surface; the
  // keybinding and the status-bar button are two more ways to press it.
  rememberTerminal(vscode.window.activeTerminal);
  context.subscriptions.push(
    vscode.window.onDidChangeActiveTerminal(rememberTerminal),
    vscode.window.onDidCloseTerminal((t) => {
      if (lastActiveTerminal === t) lastActiveTerminal = undefined;
    }),
  );
  // `darkArmy.showThisSession` is the command the manifest contributes and
  // the keybinding names. `bobCompanion.showThisSession` is its hidden alias
  // for the dual-name window: registered but not contributed, so it is absent
  // from the palette while a person's own keybindings.json entry naming the
  // old id still resolves. An older copy of this extension registers the old
  // id too, and during the swap a window that has not reloaded runs both: a
  // duplicate registration throws. That must never abort activation — the
  // server, the lock and the cleanup below are the bridge; the command is a
  // nicety — so each registration has its own try, and one refused id never
  // costs the other.
  try {
    context.subscriptions.push(
      vscode.commands.registerCommand('darkArmy.showThisSession',
        () => showThisSession()));
  } catch (err) {
    out.appendLine(`showThisSession not registered here: ${err}`);
  }
  try {
    context.subscriptions.push(
      vscode.commands.registerCommand('bobCompanion.showThisSession',
        () => showThisSession()));
  } catch (err) {
    out.appendLine(`showThisSession alias not registered here: ${err}`);
  }
  const status = vscode.window.createStatusBarItem(
    vscode.StatusBarAlignment.Right, 0);
  // The mask, not the name: this is the same 64x64 mark the panel and the
  // phone wear in their top bar, contributed as an icon font because the
  // status bar draws codicons and never images. `tools/vscode_icon_font.py`
  // bakes media/dark-army-icons.woff from assets/brand/fsociety-mark.png.
  status.text = '$(dark-army-mask)';
  status.tooltip = 'Show this session in Dark Army';
  status.command = 'darkArmy.showThisSession';
  status.show();
  context.subscriptions.push(status);

  // One line when the person trusts the folder: the gated verbs work from
  // the next request on, with no reload.
  context.subscriptions.push(
    vscode.workspace.onDidGrantWorkspaceTrust(() => {
      out.appendLine('workspace trusted: starting agents and typing into terminals are allowed here now');
    }),
  );

  context.subscriptions.push({ dispose: cleanup });
}

export function deactivate() {
  cleanup();
}

function cleanup() {
  try {
    if (lockPath && fs.existsSync(lockPath)) {
      fs.unlinkSync(lockPath);
    }
  } catch { /* best effort */ }
  lockPath = undefined;
  try {
    server?.close();
  } catch { /* best effort */ }
  server = undefined;
}

function writeLock(port: number, authToken: string) {
  try {
    const dir = ideDir();
    fs.mkdirSync(dir, { recursive: true, mode: 0o700 });
    lockPath = path.join(dir, `${port}.lock`);
    const folders = (vscode.workspace.workspaceFolders ?? []).map((f) => f.uri.fsPath);
    const body = JSON.stringify({
      port,
      pid: process.pid,
      extHostPid: process.pid,
      workspaceFolders: folders,
      // The window's *name*, for grouping agents by project rather than by the
      // directory a session happens to have been started in. In the lock rather
      // than behind an op: the daemon reads this on every agents snapshot, and a
      // fan-out of HTTP requests every few seconds to learn something that
      // changes once per window is the wrong trade. Written once, at activation
      // — a workspace renamed or a folder added mid-session is stale until the
      // window reloads, which is the same freshness the folder list has always had.
      ...workspaceIdentity(),
      ideName: vscode.env.appName,
      authToken,
      transport: 'http',
      extensionVersion: EXT_VERSION,
      // Which bridge wrote this lock: during the swap from the Bob Companion
      // extension a window can run both in one extension host, and the daemon
      // keeps one lock per host, preferring this id.
      extensionId: EXTENSION_ID,
      startedAt: Math.floor(Date.now() / 1000),
    });
    fs.writeFileSync(lockPath, body, { mode: 0o600 });
  } catch (err) {
    out.appendLine(`writeLock failed: ${err}`);
  }
}

// ---- Which terminal the reverse jump is about ----
//
// `window.activeTerminal` is the panel terminal. This machine (and anyone
// with `terminal.integrated.defaultLocation: editor`) runs agents as editor
// *tabs*, and those tabs often do not update `activeTerminal`. Jump *to* VS
// Code still works because it searches every terminal; Jump *back* used to
// send the panel's leftover pid — or none — and Dark Army opened on the wrong row
// or on none. The tab that is actually on screen is the one we mean.

let lastActiveTerminal: vscode.Terminal | undefined;

function rememberTerminal(t: vscode.Terminal | undefined): void {
  if (t) lastActiveTerminal = t;
}

function terminalForTab(tab: vscode.Tab | undefined): vscode.Terminal | undefined {
  if (!tab || !(tab.input instanceof vscode.TabInputTerminal)) return undefined;
  const terms = vscode.window.terminals;
  const label = tab.label;
  const exact = terms.filter((t) => t.name === label);
  if (exact.length) return exact.length === 1 ? exact[0] : undefined;
  // Numbered duplicates: "Grok 2". Prefer the longest name that is this
  // label, so "Grok" does not steal "Grok 2".
  let best: vscode.Terminal[] = [];
  for (const t of terms) {
    if (!t.name) continue;
    if (label === t.name || label.startsWith(t.name + ' ')) {
      if (!best.length || t.name.length > best[0].name.length) best = [t];
      else if (t.name.length === best[0].name.length) best.push(t);
    }
  }
  return best.length === 1 ? best[0] : undefined;
}

function visibleEditorTerminals(): vscode.Terminal[] {
  const out: vscode.Terminal[] = [];
  for (const group of vscode.window.tabGroups.all) {
    const t = terminalForTab(group.activeTab);
    if (t && !out.includes(t)) out.push(t);
  }
  return out;
}

function pickTerminal(): vscode.Terminal | undefined {
  const groups = vscode.window.tabGroups;
  const focusedTab = groups.activeTabGroup?.activeTab;
  // A terminal editor with an ambiguous title is still the intended target.
  // activeTerminal can describe a leftover panel, so it cannot break this tie.
  if (focusedTab?.input instanceof vscode.TabInputTerminal) {
    return terminalForTab(focusedTab);
  }
  if (groups.all.some((group) => group.activeTab?.input instanceof vscode.TabInputTerminal
      && !terminalForTab(group.activeTab))) return undefined;

  const visible = visibleEditorTerminals();
  const remembered = [vscode.window.activeTerminal, lastActiveTerminal]
    .find((t) => t && vscode.window.terminals.includes(t));
  if (remembered && (visible.length === 0 || visible.includes(remembered))) {
    return remembered;
  }
  if (visible.length === 1) return visible[0];
  return undefined;
}

// ---- Show this session in Dark Army ----
//
// This extension's only OUTBOUND call, and it is deliberately the narrowest
// thing that can be one. Three bounds, all visible here:
//   - one hardcoded action name (`reveal_panel`) — there is no op parameter a
//     caller could widen;
//   - the request carries a shell pid and a tty and nothing else. No paths, no
//     file contents, no workspace identity;
//   - the daemon treats both as untrusted *aim*: they can select a row and
//     scroll a card, never run a verb. A wrong pair highlights a stranger's row.
//
// The token is the user's own panel token, already readable by anything running
// as the user; reading it here widens reach, not privilege. The header is
// `x-bob-token`, never an Authorization header — that spelling reads fine on
// this API and silently 403s every write, a failure mode with no symptom.
async function showThisSession(): Promise<void> {
  let token: string;
  try {
    token = fs.readFileSync(tokenPath(), 'utf8').trim();
  } catch {
    return notReachable();
  }
  if (!token) return notReachable();

  // No terminal on screen is not an error: the press still means "show me Dark Army",
  // and sending neither key opens the panel plainly.
  const body: { action: string; shell_pid?: number; tty?: string } = {
    action: 'reveal_panel',
  };
  const active = pickTerminal();
  if (active) {
    const shell = await withTimeout(active.processId, 500).catch(() => undefined);
    if (typeof shell === 'number') {
      body.shell_pid = shell;
      body.tty = normalizeTty(await ttyOf(shell));
    }
  }
  out.appendLine(
    `showThisSession: pid=${body.shell_pid ?? '-'} tty=${body.tty ?? '-'} ` +
    `name=${active?.name ?? '-'}`,
  );

  let reply: { status: number; json: any };
  try {
    reply = await postToBob(body, token);
  } catch (err) {
    out.appendLine(`showThisSession: ${err}`);
    return notReachable();
  }
  if (reply.status !== 200) {
    const detail = (reply.json && reply.json.detail) || (reply.json && reply.json.error);
    vscode.window.setStatusBarMessage(
      `Dark Army: ${detail || 'could not show this session'}`, 4000);
    return;
  }
  const detail = reply.json && reply.json.detail;
  if (detail) vscode.window.setStatusBarMessage(`Dark Army: ${detail}`, 4000);
}

// One short line, never silence: the button that appears to do nothing is the
// failure this whole path exists to avoid.
function notReachable(): void {
  vscode.window.setStatusBarMessage('Dark Army is not running', 4000);
}

function postToBob(body: unknown, token: string): Promise<{ status: number; json: any }> {
  const data = Buffer.from(JSON.stringify(body));
  return new Promise((resolve, reject) => {
    const req = http.request({
      host: BOB_API_HOST,
      port: BOB_API_PORT,
      path: '/api/action',
      method: 'POST',
      timeout: 4000,
      headers: {
        'content-type': 'application/json',
        'content-length': data.length,
        // Node fills in Host: 127.0.0.1:19874, which is what the daemon's
        // anti-rebinding check wants. No Origin header: this is not a page.
        'x-bob-token': token,
      },
    }, (res) => {
      let raw = '';
      res.on('data', (chunk) => { raw += chunk; });
      res.on('end', () => {
        let parsed: any = null;
        try { parsed = JSON.parse(raw || '{}'); } catch { parsed = null; }
        resolve({ status: res.statusCode ?? 0, json: parsed });
      });
    });
    req.on('timeout', () => { req.destroy(new Error('timeout')); });
    req.on('error', reject);
    req.end(data);
  });
}

// ---- HTTP handling ----

function handleRequest(
  req: http.IncomingMessage,
  res: http.ServerResponse,
  authToken: string,
) {
  // Anti-DNS-rebind: a browser-driven request always carries Origin; a same-user
  // native client never needs to. Reject Origin outright, and require loopback Host.
  if (req.headers.origin) {
    return send(res, 403, { error: 'origin not allowed' });
  }
  const host = (req.headers.host || '').split(':')[0];
  if (host !== '127.0.0.1' && host !== 'localhost') {
    return send(res, 403, { error: 'non-loopback host' });
  }
  if (req.headers['x-bob-companion-authorization'] !== authToken) {
    return send(res, 403, { error: 'bad token' });
  }
  if (req.method !== 'POST') {
    return send(res, 405, { error: 'POST only' });
  }

  let raw = '';
  req.on('data', (chunk) => {
    raw += chunk;
    if (raw.length > 1 << 16) req.destroy(); // 64KB cap
  });
  req.on('end', () => {
    let msg: any;
    try {
      msg = JSON.parse(raw || '{}');
    } catch {
      return send(res, 400, { error: 'bad json' });
    }
    dispatch(msg, () => !req.aborted && !req.socket.destroyed && !res.destroyed)
      .then((result) => send(res, 200, result))
      .catch((err) => send(res, 500, { error: String(err) }));
  });
}

function send(res: http.ServerResponse, code: number, body: unknown) {
  const data = Buffer.from(JSON.stringify(body));
  res.writeHead(code, {
    'content-type': 'application/json',
    'content-length': data.length,
  });
  res.end(data);
}

async function dispatch(msg: any, alive: () => boolean = () => true): Promise<any> {
  switch (msg.op) {
    case 'ping':
      return {
        ok: true, version: EXT_VERSION,
        workspaceFolders: folderPaths(),
        openEditors: openEditorPaths(),
        ...workspaceIdentity(),
      };

    case 'reveal_terminal': {
      const match = await findTerminal(Number(msg.pid), String(msg.tty || ''));
      if (!match) return { matched: false };
      match.terminal.show(); // preserveFocus defaults false → focuses the tab
      return {
        matched: true,
        revealed: true,
        matchedBy: match.matchedBy,
        terminalName: match.terminal.name,
        workspaceFolders: folderPaths(),
        openEditors: openEditorPaths(),
        ...workspaceIdentity(),
      };
    }

    case 'frontmost':
      return frontmost(Array.isArray(msg.pids) ? msg.pids : []);

    case 'close_editor':
      return closeEditor(String(msg.path || ''));

    case 'send_text': {
      const text = String(msg.text || '');
      if (!text) return { matched: false, sent: false, error: 'empty text' };
      const match = await findTerminal(Number(msg.pid), String(msg.tty || ''));
      if (!match) return { matched: false, sent: false };
      // Refused while the folder is not trusted: typed text runs in a shell here.
      // Checked after the match, so only the window that owns the session says
      // so — the daemon fans this op out to every window and surfaces an
      // unmatched reply's `error`, which must not be a bystander's.
      if (!trusted()) return { matched: false, sent: false, error: UNTRUSTED };
      // No .show(): compacting must not steal the tab, let alone the window.
      match.terminal.sendText(text, msg.newline !== false);
      return {
        matched: true,
        sent: true,
        matchedBy: match.matchedBy,
        terminalName: match.terminal.name,
        ...workspaceIdentity(),
      };
    }

    case 'reply_native_terminal':
      // Refused while the folder is not trusted: a reply is typed into a terminal here.
      if (!trusted()) return { matched: false, sent: false, error: UNTRUSTED };
      return replyNativeTerminal(msg.pid, msg.tty, msg.text, msg.expires_at_ms, alive);

    case 'close_refinement_terminal':
      return closeRefinementTerminal(msg.pid, msg.tty);

    case 'close_terminal': {
      // Dispose the terminal tab owning this pid — tab and process both go
      // (VS Code SIGHUPs the process group). The daemon only asks on the
      // board's card-arrives-in-Done leg, behind its own off-by-default
      // preference; from here the request is as addressed as send_text's:
      // only the window owning the pid matches, everyone else is a no-op.
      const match = await findTerminal(Number(msg.pid), String(msg.tty || ''));
      if (!match) return { matched: false };
      const terminalName = match.terminal.name; // captured before dispose
      match.terminal.dispose();
      return {
        matched: true,
        closed: true,
        matchedBy: match.matchedBy,
        terminalName,
        ...workspaceIdentity(),
      };
    }

    case 'spawn_agent':
      // Refused while the folder is not trusted: the assistant reads its config from this workspace.
      if (!trusted()) return { spawned: false, error: UNTRUSTED };
      return spawnAgent(msg);

    default:
      return { error: `unknown op: ${msg.op}` };
  }
}

// What this window was opened AS — the only thing `code <path>` will reuse a
// window for. Three shapes, and the daemon needs to tell them apart:
//   - undefined            → a plain folder window; the daemon targets the folder.
//   - file:///…/x.code-workspace → a saved workspace; the daemon targets that file.
//   - untitled:<id>        → an untitled workspace, whose definition VS Code keeps
//                            at <userData>/Workspaces/<id>/workspace.json.
// Both the raw URI and (when it is one) the on-disk path are reported rather than
// pre-resolved here: the extension has no reliable handle on the user-data
// directory, and the daemon can check the file it is about to name actually exists.
//
// `workspaceName` is what VS Code itself calls this window, and it is the only
// shape that can name an *untitled* workspace at all — there is no file to take a
// basename from. For a saved `.code-workspace` the daemon prefers the file's own
// basename, so this is a fallback there rather than the source.
function workspaceIdentity(): {
  workspaceUri: string | null;
  workspaceFsPath: string | null;
  workspaceName: string | null;
} {
  const wf = vscode.workspace.workspaceFile;
  return {
    workspaceUri: wf ? wf.toString() : null,
    workspaceFsPath: wf && wf.scheme === 'file' ? wf.fsPath : null,
    workspaceName: vscode.workspace.name ?? null,
  };
}

// ---- spawn_agent ----
//
// Open a terminal in this window running an argv the daemon built, and show it.
// `.show()` here unlike `send_text`, which deliberately does not: this exists
// because somebody just pressed Start on a card and asked for a session, and a
// session they cannot see is the failure.
//
// **This adds no privilege the auth token did not already carry** — `send_text`
// can already type any command into a live terminal in this window, which is
// strictly more than starting one named program. It is still folder-checked, for
// a different reason: a mis-addressed request must not open a terminal in the
// wrong project. The daemon addresses this at exactly one window (see
// `vscode_reveal.spawn_agent`), and this is the second half of that promise.
//
// Nothing here is joined into a string. `shellPath` + `shellArgs` is what
// `createTerminal` spawns directly, so a prompt containing shell metacharacters
// is one argument and cannot be anything else.
//
// `shellPid` is the receipt: because the agent binary IS the terminal process,
// `terminal.processId` is the pid the daemon can later prove a session against.
// Additive — an older daemon ignores the key — and best-effort: a processId
// that has not resolved inside the cap replies null rather than holding the
// spawn reply hostage, and the daemon treats null as "no receipt".
async function spawnAgent(msg: any): Promise<{
  spawned: boolean; terminalName?: string; shellPid?: number | null;
  error?: string;
}> {
  const cwd = String(msg.cwd || '');
  const shellPath = String(msg.shellPath || '');
  if (!shellPath) return { spawned: false, error: 'no shellPath' };
  if (!cwd) return { spawned: false, error: 'no cwd' };
  // Compared through realpath on **both** sides. The daemon picked this window
  // with `os.path.realpath(folder) == os.path.realpath(root)` and then sent the
  // realpath'd root; comparing that against the raw `uri.fsPath` refused every
  // project reached through a symlink — the daemon addressed the right window
  // and the window then said no, on a project the board itself offered.
  //
  // Equal to **or contained in** a folder of this window (0.1.22): a card's
  // own worktree is `<root>/.worktrees/card-<id8>`, inside the project's
  // folder and never equal to it. Containment is component-aware — the
  // folder plus a separator — so `/a/proj` never admits `/a/project2`, and
  // anything outside every folder is refused in the same words as before.
  // A cwd that does not exist is refused: a name that resolves to nothing
  // cannot be proved to lie inside the folder, and the terminal would open
  // somewhere else.
  const target = realOrSelf(path.resolve(cwd));
  if (!fs.existsSync(target)
      || !folderPaths().map((f) => realOrSelf(path.resolve(f)))
        .some((f) => folderContains(f, target))) {
    return { spawned: false, error: 'cwd is not a folder of this window' };
  }
  const shellArgs = Array.isArray(msg.shellArgs)
    ? msg.shellArgs.map((a: unknown) => String(a))
    : [];
  // No `name`, and that is the whole reason the tab can be navigated by: a
  // terminal created with a fixed name never listens to the OSC title
  // sequence (VS Code installs the xterm title listener only for unnamed
  // terminals), so the daemon's `Gid · refine: Mobile cards` — the badge
  // every hand-started tab wears — was written into a slot nobody drew, and
  // a board-started tab read only the card's words. Unnamed, the tab wears
  // the process name for the first seconds and the daemon's title from the
  // first snapshot that knows the session's nickname. `msg.name` still
  // arrives (an older extension used it) and is echoed in `terminalName`
  // until the sequence takes over.
  // JSON-null values mean *delete* (VS Code TerminalOptions). The daemon
  // sends py2app's PYTHONHOME et al as null so a session started from the
  // frozen app does not boot pytest against the bundle's stdlib.
  const env: { [key: string]: string | null } = {};
  if (msg.env && typeof msg.env === 'object') {
    for (const [key, value] of Object.entries(msg.env)) {
      env[key] = value == null ? null : String(value);
    }
  }
  const terminal = vscode.window.createTerminal({
    cwd: target, shellPath, shellArgs,
    ...(Object.keys(env).length ? { env } : {}),
  });
  terminal.show();
  const shellPid = await withTimeout(terminal.processId, 2000)
    .catch(() => undefined);
  return {
    spawned: true,
    terminalName: terminal.name || String(msg.name || 'agent'),
    shellPid: shellPid ?? null,
  };
}

// A path resolved, or itself if it cannot be. A folder that has been deleted
// out from under an open window still has a name, and failing closed here would
// turn a resolvable request into an unexplained refusal.
function realOrSelf(p: string): string {
  try {
    return fs.realpathSync(p);
  } catch {
    return p;
  }
}

// Whether `target` is `folder` or lies inside it, by path components. Both
// sides go through `path.resolve`, so a `..` segment cannot climb out.
function folderContains(folder: string, target: string): boolean {
  const base = path.resolve(folder);
  const inner = path.resolve(target);
  if (inner === base) return true;
  const prefix = base.endsWith(path.sep) ? base : base + path.sep;
  return inner.startsWith(prefix);
}

function folderPaths(): string[] {
  return (vscode.workspace.workspaceFolders ?? []).map((f) => f.uri.fsPath);
}

function openEditorPaths(): string[] {
  const out: string[] = [];
  for (const group of vscode.window.tabGroups.all) {
    for (const tab of group.tabs) {
      const input = tab.input;
      if (input instanceof vscode.TabInputText && input.uri.scheme === 'file') {
        out.push(input.uri.fsPath);
      }
    }
  }
  return out;
}

async function closeEditor(fsPath: string): Promise<{ closed: boolean }> {
  if (!fsPath) return { closed: false };
  for (const group of vscode.window.tabGroups.all) {
    for (const tab of group.tabs) {
      const input = tab.input;
      if (input instanceof vscode.TabInputText && input.uri.fsPath === fsPath) {
        await vscode.window.tabGroups.close(tab);
        return { closed: true };
      }
    }
  }
  return { closed: false };
}

// ---- Frontmost ----
//
// "Do not interrupt me about the window I am already looking at." The daemon
// cannot answer this on its own: from outside, the frontmost *application* is
// the most it can see, and suppressing on that would silence every session in
// every VS Code window the moment one of them came forward. Only the extension
// host knows whether *this* window is focused and which terminal is on top of
// it.
//
// Two deliberate approximations, both erring towards still alerting:
//   - the ACTIVE terminal only. A session running in a background tab of the
//     focused window is not on screen, and the whole point of the rule is that
//     the message is redundant with what the user can see.
//   - `activeTerminal` stays set when the panel is hidden, and there is no API
//     that reports panel visibility. A user who hid the terminal to read code
//     will still be suppressed. That is the one false negative in here, and it
//     lasts only as long as the window stays focused.
async function frontmost(rawPids: unknown[]): Promise<{
  focused: boolean; matched: number[]; terminalName: string | null;
}> {
  if (!vscode.window.state.focused) {
    return { focused: false, matched: [], terminalName: null };
  }
  const active = vscode.window.activeTerminal;
  if (!active) return { focused: true, matched: [], terminalName: null };

  const pids = rawPids
    .map((p) => Number(p))
    .filter((p) => Number.isFinite(p) && p > 1);
  if (pids.length === 0) {
    return { focused: true, matched: [], terminalName: active.name };
  }

  const shell = await withTimeout(active.processId, 500).catch(() => undefined);
  if (typeof shell !== 'number') {
    return { focused: true, matched: [], terminalName: active.name };
  }
  const shellTty = normalizeTty(await ttyOf(shell));

  // One cache for the whole batch: several sessions in one terminal share every
  // ancestor above their own shell, and this runs on a poll.
  const ppidCache = new Map<number, number>();
  const matched: number[] = [];
  for (const pid of pids) {
    if (await ownedByShell(pid, shell, shellTty, ppidCache)) matched.push(pid);
  }
  return { focused: true, matched, terminalName: active.name };
}

async function ownedByShell(
  pid: number, shell: number, shellTty: string, cache: Map<number, number>,
): Promise<boolean> {
  let cur = pid;
  for (let depth = 0; cur > 1 && depth < 12; depth++) {
    if (cur === shell) return true;
    cur = await ppidOf(cur, cache);
    if (!cur) break;
  }
  // Same tty fallback the reveal path uses: an orphaned claude has been
  // reparented away from its shell but still shares its terminal device.
  if (!shellTty) return false;
  return normalizeTty(await ttyOf(pid)) === shellTty;
}

// ---- Terminal ownership ----

interface Match {
  terminal: vscode.Terminal;
  matchedBy: 'ancestry' | 'tty';
}

async function closeRefinementTerminal(pid: unknown, tty: unknown): Promise<any> {
  const result = await strictNativeTerminal(pid, tty, terminal => {
    const terminalName = terminal.name;
    terminal.dispose();
    return {matched: true, closed: true, matchedBy: 'ancestry-and-tty', terminalName};
  });
  return result.matched ? result : {matched: false, closed: false, reason: result.reason || ''};
}

async function replyNativeTerminal(pid: unknown, tty: unknown, text: unknown, expires: unknown, alive: () => boolean): Promise<any> {
  if (typeof expires !== 'number' || !Number.isSafeInteger(expires)
      || expires <= Date.now() || expires > Date.now() + 10000 || !alive()) {
    return {matched: false, sent: false};
  }
  if (typeof text !== 'string' || [...text].length > 2000
      || /[\u0000-\u001f\u007f-\u009f\u2028\u2029]/u.test(text)) {
    return {matched: false, sent: false};
  }
  const line = text.trim();
  if (!line || /^[/!#]/.test(line)) return {matched: false, sent: false};
  return strictNativeTerminal(pid, tty, terminal => {
    terminal.sendText('\u0015' + line, true);
    return {matched: true, sent: true, matchedBy: 'ancestry-and-tty', terminalName: terminal.name};
  }, () => alive() && Date.now() < expires);
}

async function strictNativeTerminal(pid: unknown, tty: unknown, act: (terminal: vscode.Terminal) => any, alive: () => boolean = () => true): Promise<any> {
  // Every refusal says which rung refused, so the daemon's log can tell a
  // slow probe from a real mismatch (23 Sep 2026: a stopped Codex tab could
  // not be closed under a load average near 80, and nothing said why).
  const refused = (reason: string) => ({ matched: false, closed: false, sent: false, reason });
  if (typeof pid !== 'number' || !Number.isSafeInteger(pid) || pid <= 1
      || typeof tty !== 'string' || !/^\/?(?:dev\/)?ttys[0-9]+$/.test(tty)) return refused('bad request');
  const want = normalizeTty(tty);
  // Generous on a busy Mac: each `ps` probe and each `processId` answer can
  // take hundreds of milliseconds under heavy load. The daemon waits longer
  // than this (`close_refinement_terminal`'s POST timeout), so a slow close
  // still answers rather than timing out as "may have closed".
  const deadline = Date.now() + 5000;
  // Every probe is bounded by the time left, not only checked before it
  // starts: a 1 s `ps` begun at 4.9 s used to answer at ~7 s, after the
  // daemon's 6 s wait had already read the refusal as "may have acted".
  const left = () => Math.max(0, deadline - Date.now());
  const terminals = [...vscode.window.terminals];
  const member = () => alive() && Date.now() < deadline
    && terminals.length === vscode.window.terminals.length
    && terminals.every(t => vscode.window.terminals.includes(t));
  const shells = async () => Promise.all(terminals.map(t =>
    withTimeout(t.processId, Math.min(1000, left())).catch(() => undefined)));
  const pids = await shells();
  // A terminal with no shell pid (an extension pty, or one too slow to
  // answer) cannot be an ancestor of the session, so it is left out of the
  // candidates rather than vetoing the close for every other tab. If the
  // true owner is the one that did not answer, no candidate matches and
  // the close is refused below — never aimed at a neighbour.
  const known = pids.filter((p): p is number => typeof p === 'number' && p > 1);
  if (!member()) return refused('terminals changed');
  if (new Set(known).size !== known.length) return refused('duplicate shell pid');
  let target = -1;
  // Repeat from fresh probes. The legacy ancestry/tty fallback is never called.
  for (let pass = 0; pass < 2; pass++) {
    const ancestry = new Set<number>();
    let cur: number = pid;
    const cache = new Map<number, number>();
    for (let depth = 0; cur > 1 && depth < 12 && Date.now() < deadline; depth++) {
      if (ancestry.has(cur)) return refused('process loop');
      ancestry.add(cur);
      cur = await withTimeout(ppidOf(cur, cache), left()).catch(() => 0);
    }
    if (!member()) return refused('terminals changed or deadline passed');
    const hits = pids.map((shell, i) =>
      typeof shell === 'number' && shell > 1 && ancestry.has(shell) ? i : -1).filter(i => i >= 0);
    if (hits.length !== 1) return refused(hits.length ? 'several owners' : 'no owning terminal');
    if (pass && hits[0] !== target) return refused('owner moved');
    target = hits[0];
    const owner = await withTimeout(ttyOf(pids[target]!), left()).catch(() => '');
    if (!member()) return refused('terminals changed or deadline passed');
    if (normalizeTty(owner) !== want) return refused('tty mismatch');
    if (!member()) return refused('terminals changed or deadline passed');
    const fresh = await shells();
    if (!member()) return refused('terminals changed or deadline passed');
    // Only the shells that answered the first time are compared: one that
    // answers late is new information, not a changed terminal.
    if (fresh.some((p, i) => typeof pids[i] === 'number' && p !== pids[i])) return refused('shell pid changed');
  }
  // No await between the final membership/deadline check and disposal.
  if (target < 0 || !member()) return refused('terminals changed or deadline passed');
  const terminal = terminals[target];
  return act(terminal);
}

async function findTerminal(pid: number, tty: string): Promise<Match | null> {
  const terms = vscode.window.terminals;
  if (terms.length === 0 || !pid) return null;

  // processId resolves shortly after creation; cap the wait, skip undefined
  // (extension-pty terminals never resolve one).
  const pids = await Promise.all(
    terms.map((t) => withTimeout(t.processId, 500).catch(() => undefined)),
  );
  const byPid = new Map<number, vscode.Terminal>();
  terms.forEach((t, i) => {
    const p = pids[i];
    if (typeof p === 'number') byPid.set(p, t);
  });

  // 1. ancestry: walk the claude pid up to a shell pid this window owns.
  const ppidCache = new Map<number, number>();
  let cur = pid;
  for (let depth = 0; cur > 1 && depth < 12; depth++) {
    if (byPid.has(cur)) return { terminal: byPid.get(cur)!, matchedBy: 'ancestry' };
    cur = await ppidOf(cur, ppidCache);
    if (!cur) break;
  }

  // 2. tty fallback: survives re-parenting if claude is orphaned.
  const want = normalizeTty(tty);
  if (want) {
    for (const [spid, t] of byPid) {
      if (normalizeTty(await ttyOf(spid)) === want) {
        return { terminal: t, matchedBy: 'tty' };
      }
    }
  }
  return null;
}

function ppidOf(pid: number, cache: Map<number, number>): Promise<number> {
  if (cache.has(pid)) return Promise.resolve(cache.get(pid)!);
  return ps(['-o', 'ppid=', '-p', String(pid)]).then((s) => {
    const n = parseInt(s.trim(), 10);
    const v = Number.isFinite(n) ? n : 0;
    cache.set(pid, v);
    return v;
  });
}

function ttyOf(pid: number): Promise<string> {
  return ps(['-o', 'tty=', '-p', String(pid)]);
}

function normalizeTty(t: string): string {
  return (t || '').trim().replace(/^\/dev\//, '');
}

function ps(args: string[]): Promise<string> {
  return new Promise((resolve) => {
    execFile('/bin/ps', args, { timeout: 1000 }, (err, stdout) => {
      resolve(err ? '' : stdout);
    });
  });
}

function withTimeout<T>(p: Thenable<T>, ms: number): Promise<T> {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('timeout')), ms);
    Promise.resolve(p).then(
      (v) => { clearTimeout(timer); resolve(v); },
      (e) => { clearTimeout(timer); reject(e); },
    );
  });
}
