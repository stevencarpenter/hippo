// One-shot OG image generator. Renders public/og-default.png from an SVG template.
// Run via `node scripts/generate-og.js` from site/.
import sharp from "sharp";
import { writeFileSync, readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(__dirname, "..");

const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="630" viewBox="0 0 1200 630">
  <rect width="1200" height="630" fill="#efe4ce"/>
  <rect x="40" y="40" width="1120" height="550" fill="none" stroke="#6b3c20" stroke-width="2"/>
  <rect x="80" y="76" width="120" height="28" fill="none" stroke="#6b3c20"/>
  <text x="92" y="96" font-family="Georgia, Times, serif" font-size="14" letter-spacing="6" fill="#6b3c20">PLATE I</text>
  <text x="80" y="290" font-family="Georgia, Times, serif" font-size="120" font-weight="400" fill="#2a1d10">Hippo<tspan fill="#6b3c20">·</tspan>campus.</text>
  <text x="80" y="345" font-family="Georgia, Times, serif" font-size="34" font-style="italic" fill="#6b3c20">— memoriae custos.</text>
  <text x="80" y="540" font-family="Georgia, Times, serif" font-size="22" fill="#2a1d10">A second brain that lives on your machine.</text>
  <g fill="none" stroke="#2a1d10" stroke-width="2" stroke-linecap="round" transform="translate(870,170) scale(2.6)">
    <path d="M60 18 C84 21, 99 36, 96 60 C93 81, 78 87, 66 83 C57 79, 53 71, 57 63 C60 57, 69 55, 73 60 C77 65, 75 72, 68 73"/>
    <path d="M68 73 C65 78, 60 83, 54 85 C45 88, 36 87, 33 78 C31 71, 36 66, 42 67"/>
    <path d="M60 18 C55 17, 51 19, 49 24 C48 28, 51 32, 55 33"/>
  </g>
  <text x="600" y="600" text-anchor="middle" font-family="Georgia, Times, serif" font-style="italic" font-size="16" fill="#6b3c20">fig. — cornu Ammonis · sectio sagittalis</text>
</svg>`;

const out = path.join(root, "public", "og-default.png");
await sharp(Buffer.from(svg)).png().toFile(out);
console.log(`wrote ${out}`);
