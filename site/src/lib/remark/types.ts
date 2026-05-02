import type { VFile } from "vfile";

/**
 * Frontmatter shape that Astro markdown plugins read/write through `file.data.astro.frontmatter`.
 * Use this `AstroVFile` instead of `as any` casts in remark/rehype plugins.
 */
export interface AstroVFileData {
  astro: {
    frontmatter: Record<string, unknown>;
  };
}

export type AstroVFile = VFile & { data: AstroVFileData };

export function isAstroVFile(file: VFile): file is AstroVFile {
  const data = file.data as Partial<AstroVFileData>;
  return !!data.astro && typeof data.astro === "object" && "frontmatter" in data.astro;
}
