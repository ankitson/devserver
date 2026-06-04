import { createServer, request as httpRequest, type IncomingMessage, type ServerResponse } from "node:http";
import net from "node:net";
import { createReadStream, existsSync, statSync } from "node:fs";
import { mkdir, readFile } from "node:fs/promises";
import path from "node:path";
import { spawn, type ChildProcess } from "node:child_process";

const WORKSPACE_DIR = process.env.WORKSPACE_DIR ?? "/workspace";
const CONFIG_PATH = process.env.CONFIG_PATH ?? path.join(WORKSPACE_DIR, "apps.json");
const HOST_SUFFIX = process.env.HOST_SUFFIX ?? ".dev.ankitson.com";
const ROUTER_PORT = Number(process.env.ROUTER_PORT ?? "8080");
const PORT_START = Number(process.env.PORT_START ?? "9101");
const PORT_END = Number(process.env.PORT_END ?? "9199");
const EXTRA_PATH = process.env.EXTRA_PATH;
const SCAN_INTERVAL_MS = Number(process.env.SCAN_INTERVAL_MS ?? "30000");
const MIN_RESTART_DELAY_MS = 2000;
const MAX_RESTART_DELAY_MS = 30000;

type Runtime = "bun" | "node" | "python" | "custom";
type AppType = "static" | "process";

interface AppConfig {
  type?: AppType;
  runtime?: Runtime;
  start?: string;
  install?: string;
  root?: string;
  port?: number;
  disabled?: boolean;
  env?: Record<string, string>;
  dashboard?: {
    label?: string;
    description?: string;
    category?: string;
  };
}

interface NormalizedApp extends AppConfig {
  slug: string;
  type: AppType;
  port?: number;
}

interface ManagedApp {
  app: NormalizedApp;
  key: string;
  proc: ChildProcess | null;
  restartDelay: number;
  restartTimer: ReturnType<typeof setTimeout> | null;
}

const managed = new Map<string, ManagedApp>();
let activeApps = new Map<string, NormalizedApp>();

const baseEnv = {
  ...process.env,
  ...(EXTRA_PATH ? { PATH: `${EXTRA_PATH}:${process.env.PATH ?? ""}` } : {}),
};

function log(message: string) {
  console.log(`[openclaw-app-runner] ${message}`);
}

function appLog(slug: string, message: string) {
  console.log(`[${slug}] ${message}`);
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function validateSlug(slug: string) {
  if (!/^[a-z0-9][a-z0-9-]{0,62}$/.test(slug)) {
    throw new Error(`invalid app slug "${slug}"; use lowercase letters, numbers, and hyphens`);
  }
}

function hashPort(slug: string) {
  let hash = 0;
  for (const char of slug) hash = (hash * 31 + char.charCodeAt(0)) >>> 0;
  return PORT_START + (hash % (PORT_END - PORT_START + 1));
}

function assignPort(slug: string, requested: number | undefined, used: Set<number>) {
  let port = requested ?? hashPort(slug);
  for (let i = 0; i <= PORT_END - PORT_START; i++) {
    if (port < PORT_START || port > PORT_END) port = PORT_START;
    if (!used.has(port)) {
      used.add(port);
      return port;
    }
    port += 1;
  }
  throw new Error(`no free app-runner ports in ${PORT_START}-${PORT_END}`);
}

async function loadApps() {
  const raw = await readFile(CONFIG_PATH, "utf8");
  const parsed = JSON.parse(raw);
  const source = isRecord(parsed.apps) ? parsed.apps : parsed;
  const usedPorts = new Set<number>();
  const apps = new Map<string, NormalizedApp>();

  for (const [slug, value] of Object.entries(source)) {
    if (slug.startsWith("$")) continue;
    validateSlug(slug);
    if (!isRecord(value)) throw new Error(`${slug}: config must be an object`);
    const app = value as AppConfig;
    const type: AppType = app.type ?? (app.start ? "process" : "static");
    if (type === "process" && !app.start) throw new Error(`${slug}: process apps require start`);
    const normalized: NormalizedApp = {
      ...app,
      slug,
      type,
      ...(type === "process" ? { port: assignPort(slug, app.port, usedPorts) } : {}),
    };
    apps.set(slug, normalized);
  }

  return apps;
}

function configKey(app: NormalizedApp) {
  return JSON.stringify({
    type: app.type,
    runtime: app.runtime,
    start: app.start,
    install: app.install,
    root: app.root,
    port: app.port,
    disabled: app.disabled,
    env: app.env ?? {},
  });
}

async function loadDotEnv(dir: string) {
  const env: Record<string, string> = {};
  try {
    const content = await readFile(path.join(dir, ".env"), "utf8");
    for (const line of content.split("\n")) {
      const trimmed = line.trim();
      if (!trimmed || trimmed.startsWith("#")) continue;
      const eq = trimmed.indexOf("=");
      if (eq <= 0) continue;
      env[trimmed.slice(0, eq)] = trimmed.slice(eq + 1);
    }
  } catch {
    // Optional per-app env file.
  }
  return env;
}

function resolveWorkspacePath(relativePath: string) {
  const resolved = path.resolve(WORKSPACE_DIR, relativePath);
  const workspace = path.resolve(WORKSPACE_DIR);
  if (resolved !== workspace && !resolved.startsWith(`${workspace}${path.sep}`)) {
    throw new Error(`path escapes workspace: ${relativePath}`);
  }
  return resolved;
}

function commandFor(app: NormalizedApp) {
  switch (app.runtime ?? "custom") {
    case "bun":
      return ["/usr/bin/bun", "run", app.start ?? ""];
    case "node":
      return ["node", app.start ?? ""];
    case "python":
      return ["uv", "run", app.start ?? ""];
    case "custom":
      return ["sh", "-c", app.start ?? ""];
  }
}

async function runInstall(app: NormalizedApp, dir: string, env: Record<string, string>) {
  if (!app.install) return;
  appLog(app.slug, `install: ${app.install}`);
  const proc = spawn("sh", ["-c", app.install], {
    cwd: dir,
    env,
    stdio: ["ignore", "pipe", "pipe"],
  });
  proc.stdout?.on("data", (data) => appLog(app.slug, String(data).trimEnd()));
  proc.stderr?.on("data", (data) => appLog(app.slug, String(data).trimEnd()));
  const code = await new Promise<number | null>((resolve) => proc.once("exit", resolve));
  if (code !== 0) throw new Error(`install exited with ${code}`);
}

function stopManaged(entry: ManagedApp) {
  if (entry.restartTimer) clearTimeout(entry.restartTimer);
  entry.restartTimer = null;
  if (entry.proc && !entry.proc.killed) {
    entry.proc.kill("SIGTERM");
    setTimeout(() => entry.proc?.kill("SIGKILL"), 3000).unref();
  }
  entry.proc = null;
}

function startManaged(entry: ManagedApp) {
  const app = entry.app;
  if (app.disabled || app.type !== "process") return;

  void (async () => {
    const dir = resolveWorkspacePath(app.root ?? `apps/${app.slug}`);
    const dotEnv = await loadDotEnv(dir);
    const env = {
      ...baseEnv,
      ...dotEnv,
      ...(app.env ?? {}),
      PORT: String(app.port),
      HOST: "0.0.0.0",
    };
    await runInstall(app, dir, env);
    const cmd = commandFor(app);
    appLog(app.slug, `start: ${cmd.join(" ")} on :${app.port}`);
    const proc = spawn(cmd[0], cmd.slice(1), {
      cwd: dir,
      env,
      stdio: ["ignore", "pipe", "pipe"],
    });
    entry.proc = proc;
    entry.restartDelay = MIN_RESTART_DELAY_MS;
    proc.stdout?.on("data", (data) => appLog(app.slug, String(data).trimEnd()));
    proc.stderr?.on("data", (data) => appLog(app.slug, String(data).trimEnd()));
    proc.once("exit", (code, signal) => {
      appLog(app.slug, `exited code=${code ?? ""} signal=${signal ?? ""}`);
      entry.proc = null;
      if (!managed.has(app.slug)) return;
      const delay = entry.restartDelay;
      entry.restartDelay = Math.min(delay * 2, MAX_RESTART_DELAY_MS);
      entry.restartTimer = setTimeout(() => startManaged(entry), delay);
    });
  })().catch((error) => {
    appLog(app.slug, `failed: ${error instanceof Error ? error.message : String(error)}`);
    if (!managed.has(app.slug)) return;
    const delay = entry.restartDelay;
    entry.restartDelay = Math.min(delay * 2, MAX_RESTART_DELAY_MS);
    entry.restartTimer = setTimeout(() => startManaged(entry), delay);
  });
}

async function scan() {
  let next: Map<string, NormalizedApp>;
  try {
    next = await loadApps();
  } catch (error) {
    log(`config load failed: ${error instanceof Error ? error.message : String(error)}`);
    return;
  }

  for (const [slug, entry] of managed) {
    const app = next.get(slug);
    if (!app || app.disabled || app.type !== "process" || configKey(app) !== entry.key) {
      log(`stopping ${slug}`);
      stopManaged(entry);
      managed.delete(slug);
    }
  }

  for (const [slug, app] of next) {
    if (app.disabled || app.type !== "process" || managed.has(slug)) continue;
    const entry: ManagedApp = {
      app,
      key: configKey(app),
      proc: null,
      restartDelay: MIN_RESTART_DELAY_MS,
      restartTimer: null,
    };
    managed.set(slug, entry);
    startManaged(entry);
  }

  activeApps = next;
}

function hostSlug(req: IncomingMessage) {
  const host = (req.headers.host ?? "").split(":")[0].toLowerCase();
  if (host.endsWith(HOST_SUFFIX)) return host.slice(0, -HOST_SUFFIX.length);
  if (host === "localhost" || host === "127.0.0.1") {
    const first = (req.url ?? "/").split("?")[0].split("/").filter(Boolean)[0];
    return first || "";
  }
  return "";
}

function sendJson(res: ServerResponse, status: number, body: unknown) {
  res.writeHead(status, { "Content-Type": "application/json" });
  res.end(JSON.stringify(body, null, 2));
}

function contentType(filePath: string) {
  const ext = path.extname(filePath).toLowerCase();
  return {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".ico": "image/x-icon",
    ".txt": "text/plain; charset=utf-8",
  }[ext] ?? "application/octet-stream";
}

function serveStatic(app: NormalizedApp, req: IncomingMessage, res: ServerResponse) {
  const root = resolveWorkspacePath(app.root ?? `static/${app.slug}`);
  let pathname = decodeURIComponent(new URL(req.url ?? "/", "http://localhost").pathname);
  if (hostSlug(req) === "localhost" || hostSlug(req) === "127.0.0.1") {
    pathname = `/${pathname.split("/").filter(Boolean).slice(1).join("/")}`;
  }
  let filePath = path.resolve(root, `.${pathname}`);
  if (filePath !== root && !filePath.startsWith(`${root}${path.sep}`)) {
    res.writeHead(403);
    res.end("forbidden");
    return;
  }
  if (existsSync(filePath) && statSync(filePath).isDirectory()) filePath = path.join(filePath, "index.html");
  if (!existsSync(filePath)) {
    const fallback = path.join(root, "index.html");
    if (existsSync(fallback)) filePath = fallback;
  }
  if (!existsSync(filePath) || !statSync(filePath).isFile()) {
    res.writeHead(404);
    res.end("not found");
    return;
  }
  res.writeHead(200, { "Content-Type": contentType(filePath) });
  createReadStream(filePath).pipe(res);
}

function proxyHttp(app: NormalizedApp, req: IncomingMessage, res: ServerResponse) {
  const upstream = httpRequest({
    hostname: "127.0.0.1",
    port: app.port,
    method: req.method,
    path: req.url,
    headers: { ...req.headers, host: `127.0.0.1:${app.port}` },
  }, (upstreamRes) => {
    res.writeHead(upstreamRes.statusCode ?? 502, upstreamRes.headers);
    upstreamRes.pipe(res);
  });
  upstream.on("error", (error) => {
    res.writeHead(502);
    res.end(`upstream error: ${error.message}`);
  });
  req.pipe(upstream);
}

const server = createServer((req, res) => {
  const url = new URL(req.url ?? "/", "http://localhost");
  if (url.pathname === "/_openclaw/health") {
    sendJson(res, 200, { ok: true, apps: activeApps.size });
    return;
  }
  if (url.pathname === "/_openclaw/apps") {
    sendJson(res, 200, [...activeApps.values()].map((app) => ({
      slug: app.slug,
      type: app.type,
      url: `https://${app.slug}${HOST_SUFFIX}`,
      disabled: Boolean(app.disabled),
      port: app.port,
      dashboard: app.dashboard ?? null,
    })));
    return;
  }

  const slug = hostSlug(req);
  const app = activeApps.get(slug);
  if (!app || app.disabled) {
    res.writeHead(404);
    res.end(`no OpenClaw app configured for ${slug || req.headers.host || "request"}`);
    return;
  }
  if (app.type === "static") serveStatic(app, req, res);
  else proxyHttp(app, req, res);
});

server.on("upgrade", (req, socket, head) => {
  const app = activeApps.get(hostSlug(req));
  if (!app || app.disabled || app.type !== "process" || !app.port) {
    socket.destroy();
    return;
  }
  const upstream = net.connect(app.port, "127.0.0.1", () => {
    upstream.write(
      `GET ${req.url} HTTP/${req.httpVersion}\r\n` +
      Object.entries({ ...req.headers, host: `127.0.0.1:${app.port}` })
        .map(([key, value]) => `${key}: ${Array.isArray(value) ? value.join(", ") : value ?? ""}`)
        .join("\r\n") +
      "\r\n\r\n",
    );
    if (head.length) upstream.write(head);
    socket.pipe(upstream).pipe(socket);
  });
  upstream.on("error", () => socket.destroy());
});

await mkdir(WORKSPACE_DIR, { recursive: true });
await scan();
setInterval(scan, SCAN_INTERVAL_MS).unref();
server.listen(ROUTER_PORT, "0.0.0.0", () => {
  log(`router listening on :${ROUTER_PORT}; config=${CONFIG_PATH}; host suffix=${HOST_SUFFIX}`);
});
