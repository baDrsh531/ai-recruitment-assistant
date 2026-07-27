/* ===========================================================================
   Graphiques en SVG, sans aucune bibliotheque externe.

   Amelioration progressive : le gabarit emet toujours un tableau HTML complet.
   Sans JavaScript, la page reste lisible et chaque valeur reste accessible ;
   ce fichier remplace le tableau par un graphique et laisse un bouton pour
   revenir aux donnees chiffrees.

   Specifications respectees :
     - barres de 24 px au plus, extremite arrondie, base carree ;
     - 2 px de surface entre deux marques qui se touchent, jamais de contour ;
     - grille et axes en filet plein d'un cran par-dessus la surface ;
     - legende des qu'il y a deux series, valeurs en bout de barre ;
     - texte en encre neutre, jamais a la couleur de la serie ;
     - infobulle au survol ET au clavier, sur une cible d'au moins 24 px.
   ========================================================================= */

(() => {
  "use strict";

  const NS = "http://www.w3.org/2000/svg";

  const BAR_MAX = 24; // epaisseur maximale d'une barre
  const GAP = 2; // separation en couleur de surface
  const RADIUS = 4; // arrondi de l'extremite portant la donnee
  const HIT_MIN = 24; // hauteur minimale d'une cible de pointage
  const LABEL_W = 150; // gouttiere des intitules de categories
  // Assez large pour « 12 345 tokens » : une valeur en bout de barre ne doit
  // jamais deborder du cadre.
  const VALUE_W = 84;
  const PAD = 12;
  // Au-dela, l'intitule deborderait de sa gouttiere : a 11 px, un caractere
  // occupe environ 6,2 px, et 21 caracteres remplissent deja les 130 px
  // disponibles. Le texte complet reste dans l'infobulle, dans l'etiquette
  // accessible et dans le tableau — il est raccourci a l'affichage, jamais
  // tronque par un debordement cache.
  const LABEL_MAX = 21;

  const svg = (name, attrs = {}) => {
    const node = document.createElementNS(NS, name);
    for (const [key, value] of Object.entries(attrs)) {
      node.setAttribute(key, String(value));
    }
    return node;
  };

  const ellipsize = (texte) =>
    texte.length > LABEL_MAX ? `${texte.slice(0, LABEL_MAX - 1)}…` : texte;

  function compact(value, unit) {
    let nombre;
    if (Math.abs(value) >= 10000) {
      const milliers = value / 1000;
      nombre = `${milliers.toFixed(value % 1000 === 0 ? 0 : 1)} k`;
    } else if (Number.isInteger(value)) {
      nombre = String(value);
    } else {
      nombre = value.toFixed(value < 10 ? 1 : 0);
    }
    return unit ? `${nombre} ${unit}` : nombre;
  }

  /* --- Infobulle partagee ------------------------------------------------ */
  let tip = null;

  function tooltip() {
    if (!tip) {
      tip = document.createElement("div");
      tip.className = "chart__tip";
      tip.setAttribute("role", "status");
      document.body.appendChild(tip);
    }
    return tip;
  }

  function showTip(target, lignes) {
    const node = tooltip();
    node.textContent = "";
    for (const ligne of lignes) {
      const row = document.createElement("div");
      row.className = "chart__tip-row";
      if (ligne.slot) {
        const key = document.createElement("span");
        key.className = `chart__tip-key chart__tip-key--${ligne.slot}`;
        row.appendChild(key);
      }
      // Les intitules viennent des donnees : jamais d'innerHTML.
      const valeur = document.createElement("strong");
      valeur.textContent = ligne.value;
      row.appendChild(valeur);
      const nom = document.createElement("span");
      nom.className = "chart__tip-name";
      nom.textContent = ligne.name;
      row.appendChild(nom);
      node.appendChild(row);
    }

    const box = target.getBoundingClientRect();
    node.classList.add("is-visible");
    const largeur = node.offsetWidth;
    let x = box.left + box.width / 2 - largeur / 2;
    x = Math.max(8, Math.min(x, window.innerWidth - largeur - 8));
    node.style.left = `${x + window.scrollX}px`;
    node.style.top = `${box.top + window.scrollY - node.offsetHeight - 8}px`;
  }

  const hideTip = () => tip?.classList.remove("is-visible");

  function bindTip(target, lignes) {
    target.addEventListener("pointerenter", () => showTip(target, lignes));
    target.addEventListener("pointerleave", hideTip);
    target.addEventListener("focus", () => showTip(target, lignes));
    target.addEventListener("blur", hideTip);
  }

  /* --- Barres horizontales ----------------------------------------------- */
  function renderBars(data, stacked) {
    const rows = data.rows;
    const series = data.series;
    const bandes = series.length > 1 && !stacked ? series.length : 1;

    const epaisseur = Math.min(BAR_MAX, 22);
    const hauteurBande = Math.max(HIT_MIN, epaisseur * bandes + GAP * (bandes - 1) + 14);
    const hauteur = rows.length * hauteurBande + PAD * 2;
    const largeur = 720;
    const plotX = LABEL_W;
    const plotW = largeur - LABEL_W - VALUE_W;

    const total = (row) =>
      stacked ? row.values.reduce((a, b) => a + b, 0) : Math.max(...row.values);
    const maximum = Math.max(1, ...rows.map(total));

    const racine = svg("svg", {
      viewBox: `0 0 ${largeur} ${hauteur}`,
      class: "chart__svg",
      role: "img",
      preserveAspectRatio: "xMinYMin meet",
    });

    // Ligne de base : filet plein, recessive, jamais en pointilles.
    racine.appendChild(
      svg("line", {
        x1: plotX, y1: PAD, x2: plotX, y2: hauteur - PAD,
        class: "chart__axis",
      })
    );

    rows.forEach((row, index) => {
      const haut = PAD + index * hauteurBande;
      const milieu = haut + hauteurBande / 2;

      const label = svg("text", {
        x: plotX - 10, y: milieu, class: "chart__label",
        "text-anchor": "end", "dominant-baseline": "middle",
      });
      label.textContent = ellipsize(row.label);
      if (label.textContent !== row.label) {
        const titre = svg("title");
        titre.textContent = row.label;
        label.appendChild(titre);
      }
      racine.appendChild(label);

      if (stacked) {
        let curseur = plotX;
        const somme = row.values.reduce((a, b) => a + b, 0);
        row.values.forEach((valeur, position) => {
          const largeurSegment = (valeur / maximum) * plotW;
          if (largeurSegment <= 0) return;
          const dernier = position === row.values.length - 1;
          racine.appendChild(
            barPath(
              curseur,
              milieu - epaisseur / 2,
              Math.max(1, largeurSegment - (dernier ? 0 : GAP)),
              epaisseur,
              dernier,
              `chart__mark chart__mark--${series[position].slot}`
            )
          );
          curseur += largeurSegment;
        });
        // L'unite est donnee une fois, dans le sous-titre et l'en-tete du
        // tableau. La repeter sur chaque barre serait du bruit — et la ferait
        // deborder du cadre.
        racine.appendChild(
          valueLabel(plotX + (somme / maximum) * plotW + 8, milieu, compact(somme, ""))
        );
        racine.appendChild(
          hitArea(plotX, haut, largeur - plotX, hauteurBande, row, series, data.unit)
        );
      } else {
        row.values.forEach((valeur, position) => {
          const decalage =
            bandes > 1
              ? position * (epaisseur + GAP) - ((bandes - 1) * (epaisseur + GAP)) / 2
              : 0;
          const y = milieu + decalage - epaisseur / 2;
          const largeurBarre = (valeur / maximum) * plotW;
          if (largeurBarre > 0) {
            racine.appendChild(
              barPath(
                plotX, y, largeurBarre, epaisseur, true,
                `chart__mark chart__mark--${series[position].slot}`
              )
            );
          }
          if (bandes === 1) {
            racine.appendChild(
              valueLabel(plotX + largeurBarre + 8, milieu, compact(valeur, ""))
            );
          }
        });
        racine.appendChild(
          hitArea(plotX, haut, largeur - plotX, hauteurBande, row, series, data.unit)
        );
      }
    });

    return racine;
  }

  /** Barre a extremite arrondie du cote de la donnee, carree a la base. */
  function barPath(x, y, w, h, arrondi, classe) {
    const r = arrondi ? Math.min(RADIUS, w, h / 2) : 0;
    const d = arrondi
      ? `M${x},${y} H${x + w - r} A${r},${r} 0 0 1 ${x + w},${y + r} V${y + h - r} A${r},${r} 0 0 1 ${x + w - r},${y + h} H${x} Z`
      : `M${x},${y} h${w} v${h} h${-w} Z`;
    return svg("path", { d, class: classe });
  }

  function valueLabel(x, y, texte) {
    const node = svg("text", {
      x, y, class: "chart__value", "dominant-baseline": "middle",
    });
    node.textContent = texte;
    return node;
  }

  /** Cible de pointage couvrant toute la bande, plus large que la marque. */
  function hitArea(x, y, w, h, row, series, unit) {
    const zone = svg("rect", {
      x, y, width: w, height: h,
      class: "chart__hit", tabindex: "0", role: "img",
    });
    const lignes = row.values.map((valeur, position) => ({
      slot: series[position].slot,
      value: compact(valeur, unit),
      name: series.length > 1 ? series[position].name : row.label,
    }));
    const resume = lignes.map((ligne) => `${ligne.value} ${ligne.name}`).join(", ");
    zone.setAttribute("aria-label", `${row.label} : ${resume}`);
    bindTip(zone, lignes);
    return zone;
  }

  /* --- Courbe temporelle -------------------------------------------------- */
  function renderLine(data) {
    const rows = data.rows;
    const largeur = 720;
    const hauteur = 220;
    const gauche = 48;
    const bas = 28;
    const plotW = largeur - gauche - 16;
    const plotH = hauteur - bas - PAD;
    const maximum = Math.max(1, ...rows.map((row) => row.values[0]));

    const racine = svg("svg", {
      viewBox: `0 0 ${largeur} ${hauteur}`,
      class: "chart__svg",
      role: "img",
      preserveAspectRatio: "xMinYMin meet",
    });

    // Trois graduations arrondies, en filet plein.
    for (let pas = 0; pas <= 2; pas += 1) {
      const valeur = (maximum / 2) * pas;
      const y = PAD + plotH - (valeur / maximum) * plotH;
      racine.appendChild(
        svg("line", { x1: gauche, y1: y, x2: largeur - 16, y2: y, class: "chart__grid" })
      );
      const tick = svg("text", {
        x: gauche - 8, y, class: "chart__tick",
        "text-anchor": "end", "dominant-baseline": "middle",
      });
      tick.textContent = compact(Math.round(valeur), "");
      racine.appendChild(tick);
    }

    const pointX = (index) =>
      rows.length === 1 ? gauche + plotW / 2 : gauche + (index / (rows.length - 1)) * plotW;
    const pointY = (valeur) => PAD + plotH - (valeur / maximum) * plotH;

    const trace = rows
      .map((row, index) => `${index ? "L" : "M"}${pointX(index)},${pointY(row.values[0])}`)
      .join(" ");
    racine.appendChild(svg("path", { d: trace, class: "chart__line chart__line--1" }));

    rows.forEach((row, index) => {
      const x = pointX(index);
      const y = pointY(row.values[0]);
      // Anneau de surface : le point reste lisible la ou il croise la courbe.
      racine.appendChild(svg("circle", { cx: x, cy: y, r: 6, class: "chart__ring" }));
      racine.appendChild(svg("circle", { cx: x, cy: y, r: 4, class: "chart__dot chart__dot--1" }));

      // La cible est bornee au trace : sur deux ou trois points, une demi-bande
      // depasserait largement a gauche et deborderait du cadre.
      const demi = Math.max(HIT_MIN, plotW / rows.length) / 2;
      const debut = Math.max(gauche, x - demi);
      const fin = Math.min(largeur - 16, x + demi);
      const zone = svg("rect", {
        x: debut, y: PAD, width: Math.max(1, fin - debut), height: plotH,
        class: "chart__hit", tabindex: "0", role: "img",
      });
      zone.setAttribute("aria-label", `${row.label} : ${compact(row.values[0], data.unit)}`);
      bindTip(zone, [
        { slot: 1, value: compact(row.values[0], data.unit), name: row.label },
      ]);
      racine.appendChild(zone);
    });

    // Premiere et derniere date seulement : jamais une etiquette par point.
    [0, rows.length - 1].forEach((index) => {
      if (index < 0 || (index === 0 && rows.length === 1)) return;
      const node = svg("text", {
        x: pointX(index), y: hauteur - 8, class: "chart__tick",
        "text-anchor": index === 0 ? "start" : "end",
      });
      node.textContent = rows[index].label;
      racine.appendChild(node);
    });

    return racine;
  }

  /* --- Mise en place ------------------------------------------------------ */
  function build(figure) {
    const source = figure.querySelector("script[type='application/json']");
    const canvas = figure.querySelector(".chart__canvas");
    const table = figure.querySelector(".chart__table");
    if (!source || !canvas) return;

    let data;
    try {
      data = JSON.parse(source.textContent);
    } catch {
      return; // le tableau reste affiche : rien n'est perdu
    }
    if (!data.rows?.length) return;

    canvas.appendChild(
      data.kind === "line" ? renderLine(data) : renderBars(data, data.kind === "stack")
    );

    if (data.series.length > 1) {
      const legende = document.createElement("ul");
      legende.className = "chart__legend";
      for (const item of data.series) {
        const entree = document.createElement("li");
        const puce = document.createElement("span");
        puce.className = `chart__swatch chart__swatch--${item.slot}`;
        entree.appendChild(puce);
        const nom = document.createElement("span");
        nom.textContent = item.name;
        entree.appendChild(nom);
        legende.appendChild(entree);
      }
      canvas.appendChild(legende);
    }

    if (table) {
      const bouton = figure.querySelector(".chart__toggle");
      table.hidden = true;
      if (bouton) {
        bouton.hidden = false;
        bouton.addEventListener("click", () => {
          table.hidden = !table.hidden;
          bouton.textContent = table.hidden ? "Voir les donnees" : "Masquer les donnees";
          bouton.setAttribute("aria-expanded", String(!table.hidden));
        });
      }
    }
  }

  const init = () => document.querySelectorAll("figure.chart").forEach(build);

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
