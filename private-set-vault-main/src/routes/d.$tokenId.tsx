import { createFileRoute } from "@tanstack/react-router";
import { useEffect, useMemo, useRef, useState } from "react";
import {
  Download,
  FileText,
  Copy,
  Eye,
  Loader2,
  AlertTriangle,
  Clock,
  Inbox,
  CheckCircle2,
  X,
  Lock,
} from "lucide-react";

import logoAsset from "@/assets/sets-logo.jpg.asset.json";
import {
  verifyPassword,
  fetchLink,
  fetchFileText,
  downloadFileUrl,
  downloadZipUrl,
  DEMO_HINT,
  type LinkState,
  type FileEntry,
} from "@/lib/sets-api";
import { formatBytes, formatCountdown, formatDate } from "@/lib/format";

export const Route = createFileRoute("/d/$tokenId")({
  head: () => ({
    meta: [
      { title: "Sets Downloader — Private Link" },
      { name: "robots", content: "noindex,nofollow" },
      {
        name: "description",
        content: "Securely download a private set of .txt files shared with you.",
      },
    ],
  }),
  component: Page,
});

type Phase = "locked" | "loading" | "ready";

function Page() {
  const { tokenId } = Route.useParams();
  const [phase, setPhase] = useState<Phase>("locked");
  const [session, setSession] = useState<string>("");
  const [linkState, setLinkState] = useState<LinkState | null>(null);

  useEffect(() => {
    if (phase !== "loading") return;
    let alive = true;
    (async () => {
      const s = await fetchLink(tokenId, session);
      if (!alive) return;
      setLinkState(s);
      setPhase("ready");
    })();
    return () => {
      alive = false;
    };
  }, [phase, tokenId, session]);

  return (
    <div className="relative min-h-screen overflow-hidden bg-black text-foreground">
      <BackgroundFx />
      <main className="relative z-10">
        {phase === "locked" && (
          <PasswordScreen
            tokenId={tokenId}
            onUnlock={(token) => {
              setSession(token);
              setPhase("loading");
            }}
          />
        )}
        {phase === "loading" && <CenterSpinner label="Opening secure link…" />}
        {phase === "ready" && linkState && (
          <LinkStateView
            state={linkState}
            tokenId={tokenId}
            sessionToken={session}
            onRelock={() => {
              setPhase("locked");
              setSession("");
              setLinkState(null);
            }}
          />
        )}
      </main>
    </div>
  );
}

/* -------------------- Animated black background --------------------------- */

function BackgroundFx() {
  return (
    <div aria-hidden className="pointer-events-none fixed inset-0 z-0 overflow-hidden bg-black">
      {/* faint grid */}
      <div
        className="absolute inset-0 opacity-[0.05]"
        style={{
          backgroundImage:
            "linear-gradient(to right, #fff 1px, transparent 1px), linear-gradient(to bottom, #fff 1px, transparent 1px)",
          backgroundSize: "64px 64px",
          maskImage:
            "radial-gradient(ellipse at center, black 30%, transparent 75%)",
        }}
      />
      {/* slow white aurora */}
      <div
        className="absolute left-1/2 top-1/2 h-[120vmax] w-[120vmax] -translate-x-1/2 -translate-y-1/2 animate-aurora rounded-full"
        style={{
          background:
            "radial-gradient(circle at 50% 50%, rgba(255,255,255,0.10), rgba(255,255,255,0.02) 35%, transparent 60%)",
          filter: "blur(40px)",
        }}
      />
      {/* moving shimmer band */}
      <div className="absolute inset-y-0 left-0 w-[40%] animate-shimmer">
        <div
          className="h-full w-full"
          style={{
            background:
              "linear-gradient(100deg, transparent 0%, rgba(255,255,255,0.06) 40%, rgba(255,255,255,0.14) 50%, rgba(255,255,255,0.06) 60%, transparent 100%)",
            filter: "blur(20px)",
          }}
        />
      </div>
      {/* vignette */}
      <div
        className="absolute inset-0"
        style={{
          background:
            "radial-gradient(ellipse at center, transparent 40%, rgba(0,0,0,0.85) 100%)",
        }}
      />
    </div>
  );
}

function CenterSpinner({ label }: { label: string }) {
  return (
    <div className="flex min-h-screen flex-col items-center justify-center gap-3 text-white/60">
      <Loader2 className="h-5 w-5 animate-spin" />
      <span className="text-xs tracking-wider uppercase">{label}</span>
    </div>
  );
}

/* -------------------- Password screen ------------------------------------- */

function PasswordScreen({
  tokenId,
  onUnlock,
}: {
  tokenId: string;
  onUnlock: (sessionToken: string) => void;
}) {
  const [password, setPassword] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [linkDead, setLinkDead] = useState<"expired" | "used" | "not_found" | null>(null);
  const [shake, setShake] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => inputRef.current?.focus(), []);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!password || submitting) return;
    setSubmitting(true);
    setError(null);
    const r = await verifyPassword(tokenId, password);
    setSubmitting(false);
    if (r.ok) return onUnlock(r.token);
    switch (r.reason) {
      case "wrong_password":
        setError("Incorrect password");
        setShake(true);
        setTimeout(() => setShake(false), 500);
        break;
      case "rate_limited":
        setError("Too many attempts. Wait a moment.");
        break;
      case "expired":
        setLinkDead("expired");
        break;
      case "used":
        setLinkDead("used");
        break;
      case "not_found":
        setLinkDead("not_found");
        break;
      default:
        setError("Something went wrong");
    }
  }

  if (linkDead) return <DeadLink reason={linkDead} />;

  return (
    <div className="flex min-h-screen flex-col items-center justify-center px-6">
      {/* Logo */}
      <div className="relative mb-10 animate-float">
        <div
          aria-hidden
          className="absolute inset-0 -z-10 rounded-full blur-3xl"
          style={{
            background:
              "radial-gradient(circle, rgba(255,255,255,0.18), transparent 70%)",
          }}
        />
        <div className="overflow-hidden rounded-full ring-1 ring-white/10 shadow-[0_30px_80px_-20px_rgba(0,0,0,0.9)]">
          <img
            src={logoAsset.url}
            alt="Sets"
            className="h-36 w-36 object-cover sm:h-44 sm:w-44"
            draggable={false}
          />
        </div>
      </div>

      <h1 className="mb-1 text-center text-xs font-medium uppercase tracking-[0.35em] text-white/50">
        Sets
      </h1>
      <p className="mb-10 text-center text-[11px] tracking-wider text-white/30">
        Private link
      </p>

      <form
        onSubmit={submit}
        className={
          "w-full max-w-sm transition-transform " + (shake ? "animate-[shake_0.4s]" : "")
        }
      >
        <div className="relative">
          <input
            ref={inputRef}
            type="password"
            autoComplete="current-password"
            placeholder="enter password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            disabled={submitting}
            className="w-full border-0 border-b border-white/15 bg-transparent px-1 py-3 text-center font-mono text-lg tracking-[0.4em] text-white placeholder:font-sans placeholder:text-sm placeholder:tracking-[0.2em] placeholder:lowercase placeholder:text-white/25 focus:border-white/60 focus:outline-none focus:ring-0 transition-colors"
          />
          {submitting && (
            <Loader2 className="absolute right-1 top-1/2 h-4 w-4 -translate-y-1/2 animate-spin text-white/50" />
          )}
        </div>
        {error && (
          <div className="mt-4 flex items-center justify-center gap-2 text-xs text-red-400">
            <AlertTriangle className="h-3 w-3" /> {error}
          </div>
        )}
        <p className="mt-6 text-center text-[10px] uppercase tracking-[0.3em] text-white/25">
          press enter to unlock
        </p>
      </form>

      {tokenId === "demo" || tokenId === "preview" ? (
        <p className="mt-10 text-center text-[10px] tracking-widest text-white/20">
          {DEMO_HINT}
        </p>
      ) : null}

      <style>{`
        @keyframes shake {
          0%,100% { transform: translateX(0); }
          25% { transform: translateX(-8px); }
          75% { transform: translateX(8px); }
        }
      `}</style>
    </div>
  );
}

/* -------------------- Dead link states ------------------------------------ */

function DeadLink({ reason }: { reason: "expired" | "used" | "not_found" }) {
  const map = {
    expired: {
      icon: <Clock className="h-5 w-5" />,
      title: "Link expired",
      body: "This download link is no longer valid. Ask the sender for a fresh one.",
    },
    used: {
      icon: <CheckCircle2 className="h-5 w-5" />,
      title: "Link already used",
      body: "One-time link. It has already been opened and can't be reused.",
    },
    not_found: {
      icon: <AlertTriangle className="h-5 w-5" />,
      title: "Link not found",
      body: "We couldn't find this link. Check the URL or contact the sender.",
    },
  }[reason];

  return (
    <div className="flex min-h-screen items-center justify-center px-6">
      <div className="max-w-sm text-center">
        <div className="mx-auto grid h-12 w-12 place-items-center rounded-full bg-white/5 text-white/70 ring-1 ring-white/10">
          {map.icon}
        </div>
        <h2 className="mt-5 text-base font-medium tracking-tight">{map.title}</h2>
        <p className="mt-2 text-xs text-white/50">{map.body}</p>
      </div>
    </div>
  );
}

/* -------------------- Dashboard ------------------------------------------- */

function LinkStateView({
  state,
  tokenId,
  sessionToken,
  onRelock,
}: {
  state: LinkState;
  tokenId: string;
  sessionToken: string;
  onRelock: () => void;
}) {
  if (state.status === "expired") return <DeadLink reason="expired" />;
  if (state.status === "used") return <DeadLink reason="used" />;
  if (state.status === "not_found") return <DeadLink reason="not_found" />;

  if (state.status === "empty") {
    return (
      <Shell onRelock={onRelock}>
        <CategoryHeader category={state.category} fileCount={0} totalSize={0} disabled />
        <div className="mt-8 grid place-items-center rounded-2xl border border-white/10 bg-white/[0.02] px-6 py-20 text-center">
          <Inbox className="h-7 w-7 text-white/40" />
          <h3 className="mt-3 text-sm font-medium">No files in this category</h3>
          <p className="mt-1 max-w-sm text-xs text-white/40">
            The sender hasn't uploaded anything to this category yet.
          </p>
        </div>
      </Shell>
    );
  }

  return (
    <Shell onRelock={onRelock}>
      <Dashboard
        files={state.files}
        category={state.category}
        tokenId={tokenId}
        sessionToken={sessionToken}
      />
    </Shell>
  );
}

function Shell({ children, onRelock }: { children: React.ReactNode; onRelock: () => void }) {
  return (
    <div className="mx-auto w-full max-w-5xl px-4 pb-24 pt-6 sm:pt-10">
      <header className="mb-6 flex items-center justify-between">
        <div className="flex items-center gap-3">
          <img
            src={logoAsset.url}
            alt=""
            className="h-9 w-9 rounded-full object-cover ring-1 ring-white/10"
          />
          <div className="leading-tight">
            <div className="text-sm font-medium tracking-tight">Sets</div>
            <div className="text-[10px] uppercase tracking-[0.25em] text-white/40">
              private link
            </div>
          </div>
        </div>
        <button
          onClick={onRelock}
          className="inline-flex items-center gap-1.5 rounded-full border border-white/10 px-3 py-1.5 text-[11px] text-white/60 transition hover:bg-white/5 hover:text-white"
        >
          <Lock className="h-3 w-3" /> Lock
        </button>
      </header>
      {children}
    </div>
  );
}

function CategoryHeader({
  category,
  fileCount,
  totalSize,
  disabled,
  onDownloadAll,
}: {
  category: { name: string; expiresAt?: string };
  fileCount: number;
  totalSize: number;
  disabled?: boolean;
  onDownloadAll?: () => void;
}) {
  const countdown = formatCountdown(category.expiresAt);
  return (
    <section
      className="relative overflow-hidden rounded-2xl border border-white/10 p-5 sm:p-7"
      style={{
        background:
          "linear-gradient(180deg, rgba(255,255,255,0.04), rgba(255,255,255,0.01))",
        boxShadow: "var(--shadow-card)",
      }}
    >
      <div className="flex flex-col gap-5 sm:flex-row sm:items-center sm:justify-between">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2 text-[10px] uppercase tracking-[0.3em] text-white/40">
            <span>Category</span>
            {countdown && (
              <span className="inline-flex items-center gap-1 rounded-full border border-white/10 bg-black/40 px-2 py-0.5 tracking-normal normal-case">
                <Clock className="h-2.5 w-2.5" /> expires in {countdown}
              </span>
            )}
          </div>
          <h1 className="mt-2 truncate text-xl font-semibold tracking-tight sm:text-2xl">
            {category.name}
          </h1>
          <p className="mt-1 text-xs text-white/45">
            {fileCount} file{fileCount === 1 ? "" : "s"} · {formatBytes(totalSize)}
          </p>
        </div>
        <button
          onClick={onDownloadAll}
          disabled={disabled}
          className="group relative inline-flex h-11 w-full items-center justify-center gap-2 overflow-hidden rounded-xl bg-white px-5 text-sm font-medium text-black transition hover:bg-white/90 disabled:opacity-40 sm:w-auto"
        >
          <Download className="h-4 w-4" />
          Download all as ZIP
        </button>
      </div>
    </section>
  );
}

function Dashboard({
  files,
  category,
  tokenId,
  sessionToken,
}: {
  files: FileEntry[];
  category: { name: string; expiresAt?: string };
  tokenId: string;
  sessionToken: string;
}) {
  const totalSize = useMemo(() => files.reduce((a, b) => a + b.size, 0), [files]);
  const [preview, setPreview] = useState<FileEntry | null>(null);

  function downloadAll() {
    window.location.href = downloadZipUrl(tokenId);
  }

  return (
    <div className="space-y-6">
      <CategoryHeader
        category={category}
        fileCount={files.length}
        totalSize={totalSize}
        onDownloadAll={downloadAll}
      />

      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        {files.map((f) => (
          <FileCard
            key={f.id}
            file={f}
            tokenId={tokenId}
            sessionToken={sessionToken}
            onPreview={() => setPreview(f)}
          />
        ))}
      </div>

      {preview && (
        <PreviewDrawer
          file={preview}
          tokenId={tokenId}
          sessionToken={sessionToken}
          onClose={() => setPreview(null)}
        />
      )}
    </div>
  );
}

function FileCard({
  file,
  tokenId,
  sessionToken,
  onPreview,
}: {
  file: FileEntry;
  tokenId: string;
  sessionToken: string;
  onPreview: () => void;
}) {
  const [copied, setCopied] = useState(false);
  const [copying, setCopying] = useState(false);

  async function copyText() {
    if (copying) return;
    setCopying(true);
    try {
      const text = await fetchFileText(tokenId, file.id, sessionToken);
      await navigator.clipboard.writeText(text);
      setCopied(true);
      setTimeout(() => setCopied(false), 1400);
    } catch {
      /* ignore */
    } finally {
      setCopying(false);
    }
  }

  return (
    <article
      className="group relative overflow-hidden rounded-2xl border border-white/10 bg-white/[0.02] p-4 transition hover:border-white/20 hover:bg-white/[0.04]"
      style={{ boxShadow: "var(--shadow-card)" }}
    >
      <div
        aria-hidden
        className="pointer-events-none absolute inset-x-0 top-0 h-px"
        style={{
          background:
            "linear-gradient(90deg, transparent, rgba(255,255,255,0.25), transparent)",
        }}
      />
      <div className="flex items-start gap-3">
        <div className="grid h-10 w-10 shrink-0 place-items-center rounded-xl bg-white/[0.06] text-white/80 ring-1 ring-white/10">
          <FileText className="h-4 w-4" />
        </div>
        <div className="min-w-0 flex-1">
          <div className="truncate text-sm font-medium text-white">{file.name}</div>
          <div className="mt-0.5 flex flex-wrap items-center gap-x-3 gap-y-0.5 text-[11px] text-white/40">
            <span>{formatBytes(file.size)}</span>
            <span className="text-white/20">·</span>
            <span>{formatDate(file.uploadedAt)}</span>
          </div>
        </div>
      </div>

      <div className="mt-4 flex items-center gap-1.5">
        <button
          onClick={onPreview}
          className="inline-flex h-9 flex-1 items-center justify-center gap-1.5 rounded-lg border border-white/10 bg-transparent text-xs text-white/70 transition hover:bg-white/5 hover:text-white"
        >
          <Eye className="h-3.5 w-3.5" /> Preview
        </button>
        <button
          onClick={copyText}
          className={
            "inline-flex h-9 flex-1 items-center justify-center gap-1.5 rounded-lg border border-white/10 text-xs transition hover:bg-white/5 " +
            (copied ? "text-emerald-300" : "text-white/70 hover:text-white")
          }
        >
          {copied ? <CheckCircle2 className="h-3.5 w-3.5" /> : <Copy className="h-3.5 w-3.5" />}
          {copied ? "Copied" : "Copy"}
        </button>
        <a
          href={downloadFileUrl(tokenId, file.id)}
          download={file.name}
          className="inline-flex h-9 flex-1 items-center justify-center gap-1.5 rounded-lg bg-white text-xs font-medium text-black transition hover:bg-white/90"
        >
          <Download className="h-3.5 w-3.5" /> Get
        </a>
      </div>
    </article>
  );
}

/* -------------------- Preview drawer -------------------------------------- */

function PreviewDrawer({
  file,
  tokenId,
  sessionToken,
  onClose,
}: {
  file: FileEntry;
  tokenId: string;
  sessionToken: string;
  onClose: () => void;
}) {
  const [text, setText] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    setText(null);
    setErr(null);
    fetchFileText(tokenId, file.id, sessionToken)
      .then((t) => alive && setText(t))
      .catch(() => alive && setErr("Could not load preview."));
    return () => {
      alive = false;
    };
  }, [file.id, tokenId, sessionToken]);

  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") onClose();
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <div className="fixed inset-0 z-50 flex items-end justify-center bg-black/80 backdrop-blur-sm sm:items-center">
      <div className="flex h-[85vh] w-full flex-col rounded-t-2xl border border-white/10 bg-[#0a0a0a] sm:h-[75vh] sm:max-w-3xl sm:rounded-2xl">
        <div className="flex items-center justify-between gap-3 border-b border-white/10 px-4 py-3">
          <div className="flex min-w-0 items-center gap-2.5">
            <div className="grid h-8 w-8 place-items-center rounded-lg bg-white/[0.06] ring-1 ring-white/10">
              <FileText className="h-3.5 w-3.5 text-white/80" />
            </div>
            <div className="min-w-0">
              <div className="truncate text-sm font-medium">{file.name}</div>
              <div className="text-[11px] text-white/40">
                {formatBytes(file.size)} · {formatDate(file.uploadedAt)}
              </div>
            </div>
          </div>
          <button
            onClick={onClose}
            aria-label="Close"
            className="grid h-8 w-8 place-items-center rounded-md text-white/60 transition hover:bg-white/5 hover:text-white"
          >
            <X className="h-4 w-4" />
          </button>
        </div>
        <div className="min-h-0 flex-1 overflow-auto bg-black p-4">
          {text === null && !err && (
            <div className="flex h-full items-center justify-center text-white/40">
              <Loader2 className="h-4 w-4 animate-spin" />
            </div>
          )}
          {err && <div className="text-sm text-red-400">{err}</div>}
          {text !== null && (
            <pre className="whitespace-pre-wrap break-all font-mono text-xs leading-relaxed text-white/85">
              {text}
            </pre>
          )}
        </div>
        <div className="flex items-center justify-end gap-2 border-t border-white/10 px-4 py-3">
          <a
            href={downloadFileUrl(tokenId, file.id)}
            download={file.name}
            className="inline-flex h-9 items-center gap-1.5 rounded-lg bg-white px-3 text-xs font-medium text-black transition hover:bg-white/90"
          >
            <Download className="h-3.5 w-3.5" /> Download
          </a>
        </div>
      </div>
    </div>
  );
}
