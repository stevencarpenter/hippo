import { build, context } from "esbuild";

const options = {
  entryPoints: ["src/background.ts", "src/content.ts", "src/popup.ts"],
  outdir: "dist",
  bundle: true,
  format: "iife",
  target: "es2022",
  sourcemap: false,
  minify: false,
};

if (process.argv.includes("--watch")) {
  const ctx = await context(options);
  await ctx.watch();
  console.log("watching for changes...");
} else {
  await build(options);
  console.log("built dist/");
}
