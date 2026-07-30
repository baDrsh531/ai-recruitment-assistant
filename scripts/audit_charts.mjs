/* Audit de geometrie des graphiques.
 *
 *     node scripts/audit_charts.mjs
 *
 * Rejoue le rendu de `static/js/charts.js` avec un DOM minimal et verifie
 * qu'aucune marque, etiquette ou cible de pointage ne sort du cadre. C'est le
 * controle que le validateur de palette ne fait pas : il verifie les couleurs,
 * pas la mise en page.
 *
 * Existe parce que les debordements ne se voient que sur les cas extremes —
 * un intitule tres long, une valeur a six chiffres, une courbe a deux points —
 * et qu'on ne les rencontre jamais sur les donnees de demonstration. Trois
 * defauts reels ont ete trouves par ce moyen : intitules debordant de leur
 * gouttiere, valeur en bout de barre sortant du cadre, et cible de pointage
 * partant en negatif sur une courbe a peu de points.
 */

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const RACINE = join(dirname(fileURLToPath(import.meta.url)), "..");
// Largeur moyenne d'un caractere a 11 px, mesuree sur la pile system-ui.
const CHAR_W = 6.2;

class Noeud {
  constructor(nom) {
    this.nom = nom;
    this.attrs = {};
    this.enfants = [];
    this._texte = "";
  }
  setAttribute(cle, valeur) { this.attrs[cle] = String(valeur); }
  getAttribute(cle) { return this.attrs[cle] ?? null; }
  appendChild(enfant) { this.enfants.push(enfant); return enfant; }
  addEventListener() {}
  querySelector() { return null; }
  querySelectorAll() { return []; }
  getBoundingClientRect() { return { left: 0, top: 0, width: 0, height: 0 }; }
  set textContent(valeur) { this._texte = valeur; }
  get textContent() { return this._texte; }
  get classList() { return { add() {}, remove() {} }; }
  set hidden(_valeur) {}
  get style() { return {}; }
}

globalThis.document = {
  readyState: "complete",
  createElementNS: (_ns, nom) => new Noeud(nom),
  createElement: (nom) => new Noeud(nom),
  addEventListener: () => {},
  querySelectorAll: () => [],
  body: new Noeud("body"),
};
globalThis.window = { innerWidth: 1280, scrollX: 0, scrollY: 0 };

const source = readFileSync(join(RACINE, "static/js/charts.js"), "utf8");
// eslint-disable-next-line no-eval -- audit hors production, sur un fichier du depot
eval(source.replace(
  "})();",
  "globalThis.__viz = { renderBars, renderLine, renderGauge };})();"
));

// Cas limites : c'est la qu'apparaissent les debordements, jamais sur les
// donnees de demonstration.
const CAS = [
  {
    nom: "barres, intitule tres long et valeur a six chiffres",
    data: {
      kind: "bar", unit: "tokens",
      series: [{ name: "Tokens", slot: 1 }],
      rows: [
        { label: "cv_extraction_vision_multi_colonnes", values: [123456] },
        { label: "python", values: [4] },
      ],
    },
  },
  {
    nom: "barres groupees, deux series",
    data: {
      kind: "bar", unit: "ms",
      series: [{ name: "Mediane", slot: 1 }, { name: "95e centile", slot: 2 }],
      rows: [{ label: "cv_extraction", values: [10000, 22000] }],
    },
  },
  {
    nom: "barres empilees",
    data: {
      kind: "stack", unit: "tokens",
      series: [{ name: "Entree", slot: 1 }, { name: "Generes", slot: 2 }],
      rows: [{ label: "cv_extraction", values: [1620, 2250] }],
    },
  },
  {
    nom: "barres, valeur nulle",
    data: {
      kind: "bar", unit: "candidats",
      series: [{ name: "Candidats", slot: 1 }],
      rows: [{ label: "Aucun", values: [0] }, { label: "Un", values: [1] }],
    },
  },
  {
    nom: "courbe, deux points seulement",
    data: {
      kind: "line", unit: "appels",
      series: [{ name: "Appels", slot: 1 }],
      rows: [{ label: "26/07", values: [3] }, { label: "27/07", values: [11] }],
    },
  },
  {
    nom: "courbe, point unique",
    data: {
      kind: "line", unit: "appels",
      series: [{ name: "Appels", slot: 1 }],
      rows: [{ label: "27/07", values: [5] }],
    },
  },
  // Les jauges cassent aux extremites : a 100 % un arc SVG dont le depart et
  // l'arrivee se confondent est degenere et ne dessine rien, et a 0 % il ne
  // faut tracer aucun remplissage plutot qu'un arc de longueur nulle.
  {
    nom: "jauge, proportion nulle",
    data: {
      kind: "ring", unit: "dossiers",
      series: [{ name: "Echus", slot: 1 }],
      rows: [{ label: "Echus", values: [0], total: 7, ratio: 0, target: 0,
               target_label: "objectif", invert: true }],
    },
  },
  {
    nom: "jauge, proportion pleine",
    data: {
      kind: "ring", unit: "candidatures",
      series: [{ name: "Traitees", slot: 1 }],
      rows: [{ label: "Traitees", values: [12], total: 12, ratio: 1,
               target: null, target_label: "", invert: false }],
    },
  },
  {
    nom: "jauge, seuil et grands nombres",
    data: {
      kind: "ring", unit: "candidatures",
      series: [{ name: "Au-dessus", slot: 1 }],
      rows: [{ label: "Au-dessus du seuil", values: [98765], total: 123456,
               ratio: 0.8, target: 0.85, target_label: "seuil calibre",
               invert: false }],
    },
  },
  {
    nom: "jauge, tout a zero",
    data: {
      kind: "ring", unit: "dossiers",
      series: [{ name: "Echus", slot: 1 }],
      rows: [{ label: "Echus", values: [0], total: 0, ratio: 0,
               target: null, target_label: "", invert: false }],
    },
  },
];

function inspecter(racine, W, H) {
  const problemes = [];

  const parcours = (noeud) => {
    const a = noeud.attrs;
    const nb = (cle) => (a[cle] === undefined ? null : Number(a[cle]));

    if (noeud.nom === "rect") {
      const x = nb("x"), y = nb("y"), w = nb("width"), h = nb("height");
      if (x < -0.5 || y < -0.5 || x + w > W + 0.5 || y + h > H + 0.5) {
        problemes.push(`rect hors cadre : x=${x} y=${y} w=${w} h=${h}`);
      }
      if (a.class?.includes("chart__hit") && h < 24) {
        problemes.push(`cible de pointage trop courte : ${h} px, minimum 24`);
      }
    }

    if (noeud.nom === "path" && a.d) {
      const xs = [...a.d.matchAll(/[ML](-?[\d.]+),(-?[\d.]+)/g)].map((m) => Number(m[1]));
      if (xs.some((x) => x < -0.5 || x > W + 0.5)) {
        problemes.push(`trace hors cadre : ${Math.min(...xs)}..${Math.max(...xs)}`);
      }
    }

    if (noeud.nom === "text") {
      const x = nb("x");
      const largeur = (noeud.textContent || "").length * CHAR_W;
      const ancre = a["text-anchor"];
      let gauche = x;
      if (ancre === "end") gauche = x - largeur;
      else if (ancre === "middle") gauche = x - largeur / 2;
      if (gauche < -0.5 || gauche + largeur > W + 0.5) {
        problemes.push(
          `texte « ${noeud.textContent} » deborde : ` +
            `${gauche.toFixed(0)}..${(gauche + largeur).toFixed(0)} pour ${W}`
        );
      }
    }

    if (noeud.nom === "circle") {
      const cx = nb("cx"), cy = nb("cy"), r = nb("r");
      if (cx - r < -0.5 || cx + r > W + 0.5 || cy - r < -0.5 || cy + r > H + 0.5) {
        problemes.push(`marqueur hors cadre : cx=${cx} cy=${cy} r=${r}`);
      }
    }

    noeud.enfants.forEach(parcours);
  };

  parcours(racine);
  return problemes;
}

let defauts = 0;
for (const cas of CAS) {
  const rendus = {
    line: globalThis.__viz.renderLine,
    ring: globalThis.__viz.renderGauge,
  };
  const racine = (rendus[cas.data.kind] ||
    ((d) => globalThis.__viz.renderBars(d, d.kind === "stack")))(cas.data);

  const [, , W, H] = racine.getAttribute("viewBox").split(" ").map(Number);
  const problemes = inspecter(racine, W, H);

  if (problemes.length) {
    defauts += problemes.length;
    console.log(`\n[DEFAUT] ${cas.nom}  (cadre ${W}x${H})`);
    for (const probleme of problemes) console.log(`    - ${probleme}`);
  } else {
    console.log(`[ok]     ${cas.nom}  (cadre ${W}x${H})`);
  }
}

console.log(
  defauts ? `\n${defauts} defaut(s) de geometrie.` : "\nAucun debordement detecte."
);
process.exit(defauts ? 1 : 0);
