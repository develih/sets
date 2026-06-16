import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { VaultPage } from "./routes/d.$tokenId";
import "./styles.css";

function tokenFromPath() {
  const match = window.location.pathname.match(/^\/d\/([A-Za-z0-9_-]+)/);
  return match?.[1] ?? "";
}

function App() {
  const tokenId = tokenFromPath();
  if (!tokenId) {
    return (
      <div className="grid min-h-screen place-items-center bg-black px-6 text-center text-white">
        <div>
          <h1 className="text-sm font-medium lowercase tracking-[0.35em] text-white/70">
            private set vault
          </h1>
          <p className="mt-3 text-xs lowercase tracking-[0.18em] text-white/35">
            open a private link from discord.
          </p>
        </div>
      </div>
    );
  }

  return <VaultPage tokenId={tokenId} />;
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
