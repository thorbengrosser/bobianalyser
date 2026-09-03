(function () {
  const host = document.getElementById("bb-tracker");
  if (!host) return;
  const API = host.dataset.api || (document.currentScript && new URL(document.currentScript.src).origin) || "";
  // Shadow DOM isolates the widget from the host theme's CSS (Ghost styles its
  // post-content <table>s, which otherwise bleed in). Fall back to the element
  // itself if Shadow DOM is unavailable.
  const root = host.shadowRoot || (host.attachShadow ? host.attachShadow({ mode: "open" }) : host);
  const SECTION_LABEL = { ice: "ICE", ic2: "IC/EC" };
  const imgUrl = (i) => (i.img_normal ? `${API}/api/img/${i.id}` : "");

  let all = [], settings = {}, sortBy = "chapter_name", sortDir = 1;
  const $ = (s) => root.querySelector(s);

  // ---- theme: follow the page behind the widget ----
  // Ghost themes toggle dark mode via a class/attribute on <html>, not always via
  // prefers-color-scheme, so we look at the actual computed background behind the
  // host. `data-theme="light|dark"` on #bb-tracker forces a palette.
  function parseRgb(s) {
    const m = /rgba?\(\s*([\d.]+)[ ,]+([\d.]+)[ ,]+([\d.]+)(?:[ ,/]+([\d.]+%?))?\s*\)/.exec(s || "");
    if (!m) return null;
    let a = m[4] == null ? 1 : parseFloat(m[4]); if (/%$/.test(m[4] || "")) a /= 100;
    return { r: +m[1], g: +m[2], b: +m[3], a };
  }
  function hostIsDark() {
    const forced = (host.dataset.theme || "").toLowerCase();
    if (forced === "dark" || forced === "light") return forced === "dark";
    for (let el = host.parentElement; el; el = el.parentElement) {
      const c = parseRgb(getComputedStyle(el).backgroundColor);
      if (c && c.a > 0.05) return 0.2126 * c.r + 0.7152 * c.g + 0.0722 * c.b < 128;
    }
    return !!(window.matchMedia && matchMedia("(prefers-color-scheme: dark)").matches);
  }
  function applyTheme() { host.classList.toggle("bb-dark", hostIsDark()); }
  applyTheme();
  if (window.MutationObserver) {
    const mo = new MutationObserver(applyTheme);
    const opts = { attributes: true, attributeFilter: ["class", "style", "data-theme", "data-color-scheme", "data-mode"] };
    mo.observe(document.documentElement, opts);
    if (document.body) mo.observe(document.body, opts);
  }
  if (window.matchMedia) matchMedia("(prefers-color-scheme: dark)").addEventListener("change", applyTheme);

  // ---- text helpers ----
  const _dec = document.createElement("textarea");
  function decode(s) { if (s == null) return ""; _dec.innerHTML = String(s); return _dec.value; }
  function text(s) { return decode(s).replace(/<[^>]*>/g, "").replace(/\s+/g, " ").trim(); }
  function esc(s) { return text(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])); }
  function attr(s) { return String(s == null ? "" : s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])); }
  function sanitize(html) {
    const doc = new DOMParser().parseFromString(String(html || ""), "text/html");
    doc.querySelectorAll("img, script, style, div").forEach((n) => n.remove());
    doc.body.querySelectorAll("*").forEach((n) => {
      if (!["B", "STRONG", "BR"].includes(n.tagName)) n.replaceWith(...n.childNodes);
    });
    return doc.body.innerHTML;
  }
  function parseHint(h) { return text(String(h || "").replace(/\[[^\]]*\]/g, "")); }

  function lang() { return $("#bb-lang").value; }
  function title(i) { return i["title_" + lang()] || i.title_de; }
  function desc(i) { return i["description_" + lang()] || i.description_de; }

  // ---- boot: fetch CSS + settings + items + stats in parallel ----
  Promise.all([
    fetch(`${API}/embed.css`).then((r) => r.text()).catch(() => ""),
    fetch(`${API}/api/settings`).then((r) => r.json()).catch(() => ({})),
    fetch(`${API}/api/items?available=1`).then((r) => r.json()),
    fetch(`${API}/api/stats`).then((r) => r.json()).catch(() => null),
    fetch(`${API}/api/status`).then((r) => r.json()).catch(() => null),
  ]).then(([css, s, items, stats, status]) => {
    settings = s || {};
    all = items;
    build(css, stats, status);
    render();
  });

  function build(css, stats, status) {
    if (settings.accent && host.style) host.style.setProperty("--bb-accent", settings.accent);
    const showStats = settings.show_stats !== false && stats;
    root.innerHTML = `<style>${css}</style><div class="bb-wrap">
      ${settings.heading || settings.intro ? `<div class="bb-head">
        ${settings.heading ? `<h2>${esc(settings.heading)}</h2>` : ""}
        ${settings.intro ? `<p>${esc(settings.intro)}</p>` : ""}
      </div>` : ""}
      ${showStats ? statsBar(stats) : ""}
      <div class="bb-controls">
        <input type="search" id="bb-search" placeholder="Suche…" />
        <select id="bb-section"><option value="">alle Züge</option><option value="ice">ICE</option><option value="ic2">IC/EC</option></select>
        <select id="bb-register"><option value="">Speisen & Getränke</option><option value="speisen">Speisen</option><option value="getraenke">Getränke</option></select>
        <select id="bb-flag"><option value="">alle Diät</option><option value="vegan">vegan</option><option value="vegetarian">vegetarisch</option><option value="glutenfree">glutenfrei</option><option value="lactosefree">laktosefrei</option><option value="bio">bio</option></select>
        <select id="bb-review"><option value="">alle</option><option value="1">mit Review</option><option value="0">ohne Review</option></select>
        <label class="bb-check"><input type="checkbox" id="bb-kombi"> Kombi-Menüs</label>
        <span class="bb-spacer"></span>
        <select id="bb-lang"><option value="de">DE</option><option value="en">EN</option><option value="fr">FR</option></select>
        <button class="bb-csv" id="bb-csv">⬇ CSV</button>
      </div>
      <div class="bb-count" id="bb-count"></div>
      <table>
        <thead><tr>
          <th class="bb-hide-sm"></th>
          <th data-sort="title">Titel</th>
          <th data-sort="section">Zug</th>
          <th data-sort="chapter_name" class="bb-hide-sm">Kategorie</th>
          <th data-sort="price_eur">€</th>
          <th data-sort="price_chf" class="bb-hide-sm">CHF</th>
          <th class="bb-hide-sm">Flags</th>
          <th>Review</th>
        </tr></thead>
        <tbody id="bb-tbody"></tbody>
      </table>
      ${footer(status)}
      <div class="bb-modal-bg"></div>
      <div class="bb-modal"><button class="bb-close" aria-label="Schließen">×</button><div class="bb-modal-content"></div></div>
    </div>`;
    $("#bb-lang").value = settings.default_lang || "de";
    $("#bb-kombi").checked = settings.show_kombi === true;
    const modal = root.querySelector(".bb-modal"), bg = root.querySelector(".bb-modal-bg");
    const close = () => { modal.style.display = "none"; bg.style.display = "none"; };
    root.querySelector(".bb-close").onclick = close;
    bg.onclick = close;
    ["bb-search", "bb-section", "bb-register", "bb-flag", "bb-review", "bb-lang"].forEach((id) =>
      $("#" + id).addEventListener("input", render));
    $("#bb-kombi").addEventListener("change", render);
    $("#bb-csv").addEventListener("click", exportCsv);
    root.querySelectorAll("th[data-sort]").forEach((th) => {
      th.onclick = () => { sortDir = sortBy === th.dataset.sort ? -sortDir : 1; sortBy = th.dataset.sort; render(); };
    });
  }

  function statsBar(s) {
    const cards = [
      [s.total, "Gerichte"],
      [s.price && s.price.avg != null ? "⌀ " + s.price.avg.toFixed(2) + " €" : "–", "Schnitt"],
      [s.new_7d, "neu (7 T)"],
      [s.flags ? s.flags.vegan : 0, "vegan"],
      [s.with_review, "mit Review"],
      [s.changes_30d, "Änd. (30 T)"],
    ];
    return `<div class="bb-stats">${cards.map(([v, l]) =>
      `<div class="bb-stat"><div class="v">${esc(v)}</div><div class="l">${esc(l)}</div></div>`).join("")}</div>`;
  }

  // ---- footer: data freshness from /api/status ----
  function fmtDate(iso) {
    if (!iso) return "–";
    const d = new Date(iso);
    return isNaN(d) ? esc(iso) : esc(d.toLocaleString("de-DE", { dateStyle: "medium", timeStyle: "short" }));
  }
  function footer(st) {
    if (!st) return "";
    if (!st.last_success && !st.last_attempt) {
      return `<div class="bb-foot">Noch kein Abruf protokolliert.</div>`;
    }
    let s = `Stand: <strong>${fmtDate(st.last_success)}</strong>`;
    const run = st.last_run;
    if (run && !run.ok) {
      const n = run.failed ? run.failed.length : 0;
      s += ` · <span class="bb-foot-warn" title="${attr(run.failed.map((f) => `${f.section}/${f.register}: ${f.error || ""}`).join("\n"))}">Letzter Abruf ${fmtDate(st.last_attempt)}: ${n === run.targets ? "fehlgeschlagen" : `${n} von ${run.targets} Abfragen fehlgeschlagen`}</span>`;
    } else if (st.last_attempt && st.last_attempt !== st.last_success) {
      s += ` · Letzter Abruf ${fmtDate(st.last_attempt)}`;
    }
    return `<div class="bb-foot">${s}</div>`;
  }

  function passes(i) {
    const q = $("#bb-search").value.toLowerCase();
    if (q && !(text(title(i)) + " " + text(i.chapter_name || "")).toLowerCase().includes(q)) return false;
    const sec = $("#bb-section").value; if (sec && i.section !== sec) return false;
    const reg = $("#bb-register").value; if (reg && i.register !== reg) return false;
    const flag = $("#bb-flag").value; if (flag && !(i.flags && i.flags[flag])) return false;
    const rev = $("#bb-review").value;
    if (rev === "1" && !i.blog_url) return false;
    if (rev === "0" && i.blog_url) return false;
    if (!$("#bb-kombi").checked && i.flags && i.flags.kombi) return false;
    return true;
  }

  function flagBadges(f) {
    if (!f) return "";
    return ["aktion", "neu", "vegan", "vegetarian", "glutenfree", "lactosefree", "bio"]
      .filter((k) => f[k]).map((k) => `<span class="bb-flag ${k}">${k}</span>`).join("");
  }

  function priceCell(eur, hint) {
    if (eur == null) return "";
    const old = parseHint(hint);
    return (old ? `<s>${esc(old)}</s>` : "") + eur.toFixed(2);
  }

  function filtered() {
    return all.filter(passes).sort((a, b) => {
      let av = a[sortBy], bv = b[sortBy];
      if (sortBy === "title") { av = text(title(a)); bv = text(title(b)); }
      av = av ?? ""; bv = bv ?? "";
      return av < bv ? -sortDir : av > bv ? sortDir : 0;
    });
  }

  function render() {
    const list = filtered();
    $("#bb-count").textContent = `${list.length} von ${all.length} Artikeln`;
    $("#bb-tbody").innerHTML = list.map((i) => `
      <tr data-id="${i.id}">
        <td class="bb-hide-sm">${i.img_normal ? `<img class="bb-img" loading="lazy" src="${attr(imgUrl(i))}" alt="">` : ""}</td>
        <td><div class="bb-t-title">${esc(title(i))}</div><div class="bb-t-desc">${esc(desc(i) || "")}</div></td>
        <td><span class="bb-section bb-section-${i.section}">${SECTION_LABEL[i.section] || i.section}</span></td>
        <td class="bb-hide-sm">${esc(i.chapter_name || "")}</td>
        <td class="bb-price">${priceCell(i.price_eur, i.price_hint)}</td>
        <td class="bb-price bb-hide-sm">${i.price_chf != null ? i.price_chf.toFixed(2) : ""}</td>
        <td class="bb-hide-sm"><span class="bb-flags">${flagBadges(i.flags)}</span></td>
        <td>${i.blog_url ? `<a class="bb-review" href="${attr(i.blog_url)}" target="_blank" rel="noopener noreferrer">📖 Review</a>` : ""}</td>
      </tr>`).join("");
    $("#bb-tbody").querySelectorAll("tr").forEach((tr) => (tr.onclick = () => openItem(tr.dataset.id)));
  }

  async function openItem(id) {
    const d = await fetch(`${API}/api/items/${id}`).then((r) => r.json());
    const item = d.item, v = d.versions[0] || {}, flags = v.flags || {};
    const chart = sparkline(d.versions.map((x) => x.price_eur).filter((x) => x != null).reverse());
    const modal = root.querySelector(".bb-modal");
    modal.querySelector(".bb-modal-content").innerHTML = `
      <h3>${esc(v["title_" + lang()] || v.title_de || "")}</h3>
      <p style="color:var(--bb-muted)">${esc(v["description_" + lang()] || v.description_de || "")}</p>
      ${v.extra_info_de ? `<p><em>${esc(v.extra_info_de)}</em></p>` : ""}
      <p class="bb-price">${priceCell(v.price_eur, v.price_hint)} € · ${v.price_chf != null ? v.price_chf.toFixed(2) + " CHF" : ""} ${chart}</p>
      <p><span class="bb-flags">${flagBadges(flags)}</span></p>
      ${item.blog_url ? `<p><a class="bb-review" href="${attr(item.blog_url)}" target="_blank" rel="noopener noreferrer">📖 Review auf dem Blog</a></p>` : ""}
      ${sanitize(v["ingredients_de"]) ? `<details><summary>Zutaten</summary><div class="bb-ingredients">${sanitize(v.ingredients_de)}</div></details>` : ""}
      ${d.changes.length ? `<details><summary>Änderungen (${d.changes.length})</summary>
        <ul class="bb-history">${d.changes.slice(0, 40).map((c) => `<li>${esc(c.changed_at)} — <code>${esc(c.field)}</code>: ${esc(c.old_value ?? "")} → ${esc(c.new_value ?? "")}</li>`).join("")}</ul></details>` : ""}
    `;
    modal.style.display = "block";
    root.querySelector(".bb-modal-bg").style.display = "block";
  }

  function sparkline(values) {
    if (values.length < 2) return "";
    const min = Math.min(...values), max = Math.max(...values), w = 90, h = 22;
    const pts = values.map((v, i) => {
      const x = (i / (values.length - 1)) * w;
      const y = max === min ? h / 2 : h - ((v - min) / (max - min)) * (h - 4) - 2;
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    }).join(" ");
    return `<svg width="${w}" height="${h}" style="vertical-align:middle;margin-left:6px"><polyline points="${pts}" fill="none" stroke="var(--bb-accent)" stroke-width="1.5"/></svg>`;
  }

  function exportCsv() {
    const cols = ["id", "title_de", "section", "register", "chapter_name", "price_eur", "price_chf", "blog_url"];
    const flagCols = ["vegan", "vegetarian", "bio", "glutenfree", "aktion", "neu"];
    const q = (v) => `"${String(v == null ? "" : v).replace(/"/g, '""')}"`;
    const rows = [cols.concat(flagCols).join(",")];
    for (const i of filtered()) {
      const base = cols.map((c) => q(c === "title_de" ? text(i.title_de) : i[c]));
      const fl = flagCols.map((k) => (i.flags && i.flags[k] ? "1" : "0"));
      rows.push(base.concat(fl).join(","));
    }
    const blob = new Blob(["﻿" + rows.join("\n")], { type: "text/csv;charset=utf-8" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = "bordbistro.csv";
    a.click();
    URL.revokeObjectURL(a.href);
  }
})();
