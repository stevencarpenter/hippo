import type { MotifId } from "./docs.ts";
import CornuAmmonis from "../components/motifs/CornuAmmonis.astro";
import SectioCoronalis from "../components/motifs/SectioCoronalis.astro";
import TrisynapticCircuit from "../components/motifs/TrisynapticCircuit.astro";
import MarginaliaMotif from "../components/motifs/MarginaliaMotif.astro";
import PlateFrame from "../components/motifs/PlateFrame.astro";
import Fasciculus from "../components/motifs/Fasciculus.astro";

export const motifs = {
  "cornu-ammonis": CornuAmmonis,
  "sectio-coronalis": SectioCoronalis,
  "trisynaptic-circuit": TrisynapticCircuit,
  "marginalia": MarginaliaMotif,
  "plate-frame": PlateFrame,
  "fasciculus": Fasciculus,
} as const;

/** Caption strings for each motif in italic Junicode (decorative — aria-hidden via the component). */
export const motifCaptions: Record<MotifId, string> = {
  "cornu-ammonis": "cornu Ammonis",
  "sectio-coronalis": "sectio coronalis",
  "trisynaptic-circuit": "trisynaptic circuit",
  "marginalia": "marginalia",
  "plate-frame": "—",
  "fasciculus": "fasciculus",
};

export function getMotif(id: MotifId) {
  return motifs[id];
}
