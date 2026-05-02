import rss from "@astrojs/rss";
import type { APIContext } from "astro";
import { listReleases } from "../../lib/github.ts";

export async function GET(context: APIContext) {
  const releases = await listReleases();
  return rss({
    title: "hippo — changelog",
    description: "Releases of hippo.",
    site: context.site ?? "https://hippobrain.org",
    items: releases.map((r) => ({
      title: r.name || r.tag,
      pubDate: r.date ? new Date(`${r.date}T00:00:00Z`) : new Date(),
      description: r.body.slice(0, 500),
      link: r.url,
    })),
    customData: `<language>en-us</language>`,
  });
}
