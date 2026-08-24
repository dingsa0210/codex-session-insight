// Dev-only middleware: read/write the shared exclusion list from the Node side.
// The app runs inside workerd, whose node:fs can only touch ephemeral /tmp —
// so persistence must happen here, in the Vite dev server process.
import { mkdir, readFile, rename, writeFile } from "node:fs/promises";
import type { ServerResponse } from "node:http";
import path from "node:path";
import type { Connect, Plugin } from "vite";

export const EXCLUSIONS_FILE = path.resolve(process.cwd(), "data", "excluded_projects.json");

async function readExclusions(): Promise<string[]> {
  try {
    const payload = JSON.parse(await readFile(EXCLUSIONS_FILE, "utf8"));
    const entries = payload?.excluded;
    return Array.isArray(entries) ? entries.filter((item): item is string => typeof item === "string") : [];
  } catch {
    return [];
  }
}

function sendJson(res: ServerResponse, body: unknown, status = 200): void {
  res.statusCode = status;
  res.setHeader("content-type", "application/json");
  res.end(JSON.stringify(body));
}

async function handleExclusions(req: Connect.IncomingMessage, res: ServerResponse): Promise<void> {
  if (req.method === "GET") {
    sendJson(res, { excluded: await readExclusions() });
    return;
  }
  if (req.method === "POST") {
    let raw = "";
    req.on("data", (chunk: Buffer) => { raw += chunk; });
    await new Promise<void>((resolve, reject) => {
      req.on("end", resolve);
      req.on("error", reject);
    });
    let payload: any;
    try {
      payload = JSON.parse(raw || "null");
    } catch {
      payload = null;
    }
    const entries = payload?.excluded;
    if (!Array.isArray(entries) || entries.some((item: unknown) => typeof item !== "string" || !(item as string).trim())) {
      sendJson(res, { error: 'body 须为 {"excluded": string[]}，元素须为非空字符串' }, 400);
      return;
    }
    const excluded = [...new Set(entries.map((item: string) => item.trim()))];
    await mkdir(path.dirname(EXCLUSIONS_FILE), { recursive: true });
    const tmpPath = `${EXCLUSIONS_FILE}.tmp`;
    await writeFile(tmpPath, `${JSON.stringify({ version: 1, updated_at: new Date().toISOString(), excluded }, null, 2)}\n`, "utf8");
    await rename(tmpPath, EXCLUSIONS_FILE);
    sendJson(res, { ok: true, excluded });
    return;
  }
  sendJson(res, { error: "method not allowed" }, 405);
}

export function exclusionsServer(): Plugin {
  return {
    name: "exclusions-server",
    apply: "serve",
    configureServer(server) {
      server.middlewares.use("/api/exclusions", (req, res) => {
        handleExclusions(req as Connect.IncomingMessage, res as ServerResponse).catch(() => sendJson(res as ServerResponse, { error: "internal error" }, 500));
      });
    },
  };
}
