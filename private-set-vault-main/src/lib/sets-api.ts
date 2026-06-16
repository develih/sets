// Frontend API client for private set vault.
// Uses RELATIVE URLs so it works on whatever domain the app is served from.
// When the backend is not yet wired up, falls back to a built-in demo dataset
// so the UI can be previewed end-to-end.

export type FileEntry = {
  id: string;
  name: string;
  size: number; // bytes
  uploadedAt?: string; // ISO
  preview?: string; // optional cached text preview
};

export type CategoryMeta = {
  name: string;
  fileCount: number;
  totalSize: number;
  expiresAt?: string;
};

export type LinkState =
  | { status: "ok"; category: CategoryMeta; files: FileEntry[] }
  | { status: "expired" }
  | { status: "used" }
  | { status: "not_found" }
  | { status: "empty"; category: CategoryMeta };

type VerifyResult =
  | { ok: true; token: string }
  | { ok: false; reason: "wrong_password" | "expired" | "used" | "not_found" | "rate_limited" | "server" };

const DEMO_TOKENS = new Set(["demo", "preview"]);
const DEMO_PASSWORD = "demo1234";

function isDemo(tokenId: string) {
  return DEMO_TOKENS.has(tokenId);
}

async function safeFetch(input: string, init?: RequestInit) {
  try {
    const res = await fetch(input, init);
    return res;
  } catch {
    return null;
  }
}

export async function verifyPassword(tokenId: string, password: string): Promise<VerifyResult> {
  const res = await safeFetch(`/api/download/${encodeURIComponent(tokenId)}/verify`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ password }),
  });

  if (res) {
    if (res.ok) {
      const data = (await res.json().catch(() => ({}))) as { sessionToken?: string };
      return { ok: true, token: data.sessionToken ?? "" };
    }
    if (res.status === 401) return { ok: false, reason: "wrong_password" };
    if (res.status === 404) return { ok: false, reason: "not_found" };
    if (res.status === 410) return { ok: false, reason: "expired" };
    if (res.status === 409) return { ok: false, reason: "used" };
    if (res.status === 429) return { ok: false, reason: "rate_limited" };
    return { ok: false, reason: "server" };
  }

  // Backend not reachable -> demo fallback
  if (isDemo(tokenId)) {
    await new Promise((r) => setTimeout(r, 400));
    if (password === DEMO_PASSWORD) return { ok: true, token: "demo-session" };
    return { ok: false, reason: "wrong_password" };
  }
  return { ok: false, reason: "not_found" };
}

export async function fetchLink(tokenId: string, sessionToken: string): Promise<LinkState> {
  const res = await safeFetch(`/api/download/${encodeURIComponent(tokenId)}/files`, {
    headers: { Authorization: `Bearer ${sessionToken}` },
  });
  if (res) {
    if (res.status === 410) return { status: "expired" };
    if (res.status === 409) return { status: "used" };
    if (res.status === 404) return { status: "not_found" };
    if (res.ok) {
      const data = (await res.json()) as { category: CategoryMeta; files: FileEntry[] };
      if (!data.files?.length) return { status: "empty", category: data.category };
      return { status: "ok", category: data.category, files: data.files };
    }
  }

  if (isDemo(tokenId)) {
    return demoState();
  }
  return { status: "not_found" };
}

export async function fetchFileText(tokenId: string, fileId: string, sessionToken: string): Promise<string> {
  const res = await safeFetch(`/api/download/${encodeURIComponent(tokenId)}/files/${encodeURIComponent(fileId)}/raw`, {
    headers: { Authorization: `Bearer ${sessionToken}` },
  });
  if (res && res.ok) return res.text();
  if (isDemo(tokenId)) {
    const f = DEMO_FILES.find((x) => x.id === fileId);
    return f?.preview ?? "";
  }
  throw new Error("Could not load file");
}

export function downloadFileUrl(tokenId: string, fileId: string) {
  return `/api/download/${encodeURIComponent(tokenId)}/files/${encodeURIComponent(fileId)}/download`;
}

export function downloadZipUrl(tokenId: string) {
  return `/api/download/${encodeURIComponent(tokenId)}/zip`;
}

// ----- Demo data ------------------------------------------------------------
const DEMO_CATEGORY: CategoryMeta = {
  name: "streetwear drops - week 24",
  fileCount: 6,
  totalSize: 0,
  expiresAt: new Date(Date.now() + 1000 * 60 * 60 * 22).toISOString(),
};

const DEMO_FILES: FileEntry[] = [
  {
    id: "f1",
    name: "supreme-ss26-mainlines.txt",
    size: 18_432,
    uploadedAt: "2026-06-14T09:14:00Z",
    preview:
      "https://supreme.com/products/...\nhttps://supreme.com/products/...\nhttps://supreme.com/products/...\n",
  },
  {
    id: "f2",
    name: "nike-snkrs-eu.txt",
    size: 9_120,
    uploadedAt: "2026-06-14T10:02:00Z",
    preview: "SKU,Variant,Region\nDV1234-100,US 9,EU\nDV1234-100,US 9.5,EU\n",
  },
  {
    id: "f3",
    name: "footlocker-keywords.txt",
    size: 2_240,
    uploadedAt: "2026-06-15T07:41:00Z",
    preview: "+jordan,+retro,+travis\n+yeezy,+slide\n",
  },
  {
    id: "f4",
    name: "shopify-quicktasks.txt",
    size: 54_700,
    uploadedAt: "2026-06-15T13:20:00Z",
    preview: "https://shop.example.com/products/item-a\nhttps://shop.example.com/products/item-b\n",
  },
  {
    id: "f5",
    name: "proxies-residential.txt",
    size: 124_980,
    uploadedAt: "2026-06-15T18:05:00Z",
    preview: "user:pass@gate.proxy.io:7777\nuser:pass@gate.proxy.io:7778\n",
  },
  {
    id: "f6",
    name: "monitors-discord.txt",
    size: 3_310,
    uploadedAt: "2026-06-16T02:11:00Z",
    preview: "https://discord.gg/xxx\nhttps://discord.gg/yyy\n",
  },
];

function demoState(): LinkState {
  const totalSize = DEMO_FILES.reduce((a, b) => a + b.size, 0);
  return {
    status: "ok",
    category: { ...DEMO_CATEGORY, fileCount: DEMO_FILES.length, totalSize },
    files: DEMO_FILES,
  };
}
