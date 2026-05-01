// @ts-check
import { defineConfig } from "astro/config";
import sitemap from "@astrojs/sitemap";
import pagefind from "astro-pagefind";
import rehypeSlug from "rehype-slug";
import rehypeAutolinkHeadings from "rehype-autolink-headings";
import { rehypeLinkRewrite } from "./src/lib/rehype/link-rewrite.ts";
import { rehypeImageResolve } from "./src/lib/rehype/image-resolve.ts";
import { remarkEditOnGithub } from "./src/lib/remark/edit-on-github.ts";
import { copyDocImages } from "./integrations/copy-doc-images.ts";

// https://astro.build/config
export default defineConfig({
  site: "https://hippobrain.org",
  output: "static",
  trailingSlash: "ignore",
  experimental: {
    contentLayer: true,
  },
  integrations: [
    sitemap(),
    pagefind(),
    copyDocImages(),
  ],
  markdown: {
    syntaxHighlight: "shiki",
    shikiConfig: {
      theme: "css-variables",
      wrap: false,
    },
    remarkPlugins: [remarkEditOnGithub],
    rehypePlugins: [
      rehypeLinkRewrite,
      rehypeImageResolve,
      rehypeSlug,
      [
        rehypeAutolinkHeadings,
        {
          behavior: "append",
          properties: { className: ["anchor"], "aria-hidden": "true", tabIndex: -1 },
          // Empty text — the ¶ glyph is rendered via CSS ::before so it never
          // pollutes heading.text in either tabs or in `headings[]`.
          content: { type: "text", value: "" },
        },
      ],
    ],
  },
  vite: {
    server: {
      fs: { allow: [".."] },
      watch: {
        ignored: ["!../docs/**", "!../README.md", "!../CONTRIBUTING.md"],
      },
    },
  },
});
