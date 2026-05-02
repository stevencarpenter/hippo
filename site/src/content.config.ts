import { defineCollection, z } from "astro:content";
import { glob } from "astro/loaders";
import { docsLoader, singleMarkdownLoader } from "./lib/markdown-file-loader.ts";

/** Tier-2 docs sourced from ../docs/**\/*.md (excluding archive + superpowers). */
const docs = defineCollection({
  loader: docsLoader(),
  schema: z.object({
    title: z.string().optional(),
    description: z.string().optional(),
    order: z.number().optional(),
    sourcePath: z.string().optional(),
  }),
});

/** README.md → /docs/getting-started. */
const rootDocs = defineCollection({
  loader: singleMarkdownLoader({
    filePath: "../README.md",
    id: "getting-started",
    sourcePath: "README.md",
  }),
  schema: z.object({
    title: z.string().optional(),
    description: z.string().optional(),
    sourcePath: z.string().optional(),
  }),
});

/** CONTRIBUTING.md → /docs/contributing. */
const contributing = defineCollection({
  loader: singleMarkdownLoader({
    filePath: "../CONTRIBUTING.md",
    id: "contributing",
    sourcePath: "CONTRIBUTING.md",
  }),
  schema: z.object({
    title: z.string().optional(),
    description: z.string().optional(),
    sourcePath: z.string().optional(),
  }),
});

/** Field notes — hand-written markdown blog posts. */
const blog = defineCollection({
  loader: glob({ pattern: "**/*.md", base: "./src/content/blog" }),
  schema: z.object({
    title: z.string(),
    date: z.coerce.date(),
    description: z.string(),
    motif: z.enum([
      "cornu-ammonis",
      "sectio-coronalis",
      "trisynaptic-circuit",
      "marginalia",
      "plate-frame",
      "fasciculus",
    ]), // U6: required, no default
    draft: z.boolean().default(false),
  }),
});

export const collections = { docs, rootDocs, contributing, blog };
