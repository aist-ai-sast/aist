import { readFileSync } from "node:fs";

import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig(({ mode }) => {
  if (mode === "csrf-runtime") {
    return {
      build: {
        outDir: "../aist/static/aist/js",
        emptyOutDir: false,
        minify: false,
        lib: {
          entry: "src/lib/csrf-runtime.js",
          formats: ["iife"],
          name: "AistCsrf",
          fileName: () => "csrf.js",
        },
        rollupOptions: {
          output: {
            banner: "/*! Generated from client-ui/src/lib/csrf-runtime.js by npm run build; do not edit.\n" +
              readFileSync("node_modules/js-cookie/LICENSE", "utf8") + " */",
          },
        },
      },
    };
  }
  return {
    plugins: [react()],
    build: {
      outDir: "dist",
      emptyOutDir: true,
      manifest: "assets/manifest.json",
      sourcemap: true,
      minify: false,
      rollupOptions: {
        output: {
          entryFileNames: "assets/[name]-[hash].js",
          chunkFileNames: "assets/chunk-[name]-[hash].js",
          assetFileNames: "assets/[name]-[hash][extname]",
        },
      },
    },
  };
});
