import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // NEXT_DIST_DIR lets a local production build (e.g. .next-prod) run next to
  // a dev server without overwriting its .next folder. Vercel leaves it unset.
  distDir: process.env.NEXT_DIST_DIR || ".next",
  turbopack: {
    root: process.cwd(),
  },
};

export default nextConfig;
