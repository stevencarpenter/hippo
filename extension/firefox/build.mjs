import { build, context } from "esbuild";

const common = {
  bundle: true,
  format: "iife",
  target: "es2022",
  sourcemap: false,
  minify: false,
};

const entries = [
  { entryPoints: ["src/background.ts"], outfile: "dist/background.js" },
  { entryPoints: ["src/content.ts"], outfile: "dist/content.js" },
  { entryPoints: ["src/popup.ts"], outfile: "dist/popup.js" },
];

if (process.argv.includes("--watch")) {
  for (const entry of entries) {
    const ctx = await context({ ...common, ...entry });
    await ctx.watch();
  }
  console.log("watching for changes...");
} else {
  await Promise.all(entries.map((e) => build({ ...common, ...e })));
  console.log("built dist/");
}
