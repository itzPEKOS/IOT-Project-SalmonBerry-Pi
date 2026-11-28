// รูป pixel art วาดจากตาราง ตัวอักษรหนึ่งตัว = หนึ่งช่อง ("." = โปร่งใส)

const COLORS = {
  H: "#5a3a22", S: "#f5c89a", E: "#0e0f1f", B: "#47d1ff", b: "#2aa7d6", P: "#2c2f6e", W: "#f4f1e8",
  R: "#ff3b4e", r: "#c22a3a", w: "#ffd1d7", Y: "#ffc93c", y: "#c98b1d", G: "#3be36b", K: "#05060f",
  L: "#8f93c9", C: "#d9e7ff", N: "#24275a",
};

const MAPS = {
  runner: [
    "....HHHHH...",
    "...HHHHHHH..",
    "...HSSSSSH..",
    "...SSSSESS..",
    "....SSSSS...",
    "..BBBBBBBB..",
    ".SBBBBBBBBS.",
    ".S.BBBBBB.S.",
    "...BBBBBB...",
    "...PPPPPP...",
    "..PPP..PPP..",
    "..PP....PP..",
    ".PP......PP.",
    ".WW......WW.",
  ],
  runner2: [
    "....HHHHH...",
    "...HHHHHHH..",
    "...HSSSSSH..",
    "...SSSSESS..",
    "....SSSSS...",
    "..BBBBBBBB..",
    "..BBBBBBBBSS",
    ".SSBBBBBB...",
    "...BBBBBB...",
    "...PPPPPP...",
    "...PPPPP....",
    "...PP.PP....",
    "..PP..PP....",
    "..WW..WW....",
  ],
  heart: [
    ".RRR...RRR.",
    "RwwRR.RRRRR",
    "RwRRRRRRRRR",
    "RRRRRRRRRRR",
    "RRRRRRRRRRR",
    ".RRRRRRRRR.",
    "..RRRRRRR..",
    "...RRRRR...",
    "....RRR....",
    ".....R.....",
  ],
  trophy: [
    "YY.YYYYYY.YY",
    "Y.YYWYYYYY.Y",
    "Y.YWYYYYYY.Y",
    "YY.YYYYYY.YY",
    "...YYYYYY...",
    "....YYYY....",
    ".....YY.....",
    ".....YY.....",
    "...yyyyyy...",
    "..yyyyyyyy..",
  ],
  skull: [
    "..WWWWWWW..",
    ".WWWWWWWWW.",
    "WWWWWWWWWWW",
    "WKKWWWWKKWW",
    "WKKWWWWKKWW",
    "WWWWWKWWWWW",
    ".WWWWWWWWW.",
    "..WKWKWKW..",
    "..WWWWWWW..",
  ],
  flag: [
    "LWKWKW",
    "LKWKWK",
    "LWKWKW",
    "L.....",
    "L.....",
    "L.....",
    "L.....",
  ],
  camera: [
    "LLLLLLLLL",
    "LCCCCCCCL",
    "LCKKCCCCL",
    "LCKBCCCCL",
    "LCCCCCRCL",
    "LLLLLLLLL",
    "....L....",
    "...LLL...",
  ],
};

export function sprite(name, scale = 4, extraClass = "") {
  const rows = MAPS[name];
  if (!rows) return "";
  const width = Math.max(...rows.map((r) => r.length));
  let rects = "";
  rows.forEach((row, y) => {
    [...row].forEach((ch, x) => {
      if (COLORS[ch]) rects += `<rect x="${x}" y="${y}" width="1.02" height="1.02" fill="${COLORS[ch]}"/>`;
    });
  });
  return `<svg class="sprite ${extraClass}" viewBox="0 0 ${width} ${rows.length}" width="${width * scale}" height="${rows.length * scale}" shape-rendering="crispEdges" aria-hidden="true">${rects}</svg>`;
}

/** ใส่ sprite ให้ทุก element ที่มี data-sprite */
export function fillSprites(root = document) {
  root.querySelectorAll("[data-sprite]").forEach((el) => {
    el.innerHTML = sprite(el.dataset.sprite, Number(el.dataset.scale || 4));
  });
}
