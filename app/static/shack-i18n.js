"use strict";
/* shack-i18n.js: the web UI's words and numbers (openhamshack
   docs/contract/I18N.md section 2, Web UIs, and section 4).

   The contract names this file as ohs-libs/js/shack-i18n.js, copied into
   each app. ohs-libs has no js/ yet, so this copy in OpenHamShack Icom is
   the first one; OpenHamShack Flex and Digital Modes carry the same file. When it moves to
   ohs-libs, both become copies of that and say which commit.

   Three jobs, and nothing fetched from anywhere but this app:

   1. A catalog. static/i18n/en.json is the English source; another
      language is static/i18n/<tag>.json beside it. Keys nest ("conn":
      {"connect": ...} is the key "conn.connect"), and a plural is an object
      of CLDR categories ({"one": "{n} client", "other": "{n} clients"})
      chosen by Intl.PluralRules, never by n === 1 in the caller.
   2. t(key, vars): the sentence with its {named} placeholders filled. A
      missing key gives the key back rather than throwing, so a typo shows
      up on screen as "conn.conect" instead of a blank page; the check
      (tools/i18n.py check) refuses a key the English catalog lacks.
   3. fmt: frequencies and numbers in the viewer's locale, byte for byte
      what ohs-libs spec/vectors/i18n/format.json says, so a frequency reads
      the same here as in the Kotlin, Swift and Python helpers.

   Decisions:

   - The decimal, group and minus symbols come from a table (the same
     CLDR-derived one as ohs-libs' Python _cldr.py and Kotlin ShackFormat),
     not from Intl.NumberFormat. Intl's answer is whatever ICU the browser
     shipped with: an older Chromium on a Pi kiosk or a TV's web view groups
     French with a different space than a current desktop, and "de-LU" or
     "pt_BR.UTF-8" either throw or fall back to the browser's own locale.
     One table gives one answer everywhere and lets the shared vectors pin
     it. Intl is used for plural categories only, where every browser in
     the field agrees for the languages we would ship.
   - Arithmetic is decimal (BigInt digits and a scale), never binary
     floating point, so 2.675 to two places is 2.68 and 14074.05 kHz rounds
     the way a person expects: ties go away from zero.
   - The catalog loads asynchronously; ShackI18n.ready resolves when it is
     in. Pages keep their English in the markup (data-i18n replaces it), so
     nothing flashes while it loads; scripts that write text wait for ready
     before their first draw.
   - Language: ?lang= on the page's address (for testing a catalog), then a
     choice saved in this browser (localStorage "shack.lang"), then the
     browser's own list. English is always loaded underneath, so a key a
     translation has not caught up with shows in English, not as a key. */

(function (root) {
  const NBSP = " ";

  /* ---------------------------------------------------------- symbols --- */

  const HY = "-", MI = "−", NB = " ", NN = " ";
  const sym = (decimal, group, minus) => ({ decimal, group, minus });
  /* Add a locale here and in ohs-libs (_cldr.py, ShackFormat.kt,
     ShackFormat.swift) together, or the ports disagree. */
  const TABLE = {
    "en": sym(".", ",", HY), "en-GB": sym(".", ",", HY), "en-US": sym(".", ",", HY),
    "de": sym(",", ".", HY), "de-AT": sym(",", NB, HY), "de-CH": sym(".", "’", HY),
    "fr": sym(",", NN, HY), "fr-CA": sym(",", NB, HY),
    "es": sym(",", ".", HY), "es-MX": sym(".", ",", HY),
    "it": sym(",", ".", HY), "nl": sym(",", ".", HY),
    "nb": sym(",", NB, MI), "nn": sym(",", NB, MI), "no": sym(",", NB, MI),
    "sv": sym(",", NB, MI), "da": sym(",", ".", HY), "fi": sym(",", NB, MI),
    "pl": sym(",", NB, HY),
    // CLDR's plain "pt" is Brazilian Portuguese; Portugal is the variant.
    "pt": sym(",", ".", HY), "pt-BR": sym(",", ".", HY), "pt-PT": sym(",", NB, HY),
    "ja": sym(".", ",", HY), "zh": sym(".", ",", HY),
    "ru": sym(",", NB, HY), "uk": sym(",", NB, HY), "cs": sym(",", NB, HY),
  };

  /* Exact tag, then language, then en. POSIX spellings (de_DE.UTF-8) lose
     the encoding and modifier; an unlisted region takes its language's. */
  function symbols(locale) {
    if (!locale) return TABLE.en;
    const tag = String(locale).split(".")[0].split("@")[0].replace(/_/g, "-");
    const parts = tag.split("-").filter(Boolean);
    if (!parts.length) return TABLE.en;
    const lang = parts[0].toLowerCase();
    const region = parts.slice(1).find((p) => /^[A-Za-z]{2}$/.test(p));
    if (region && TABLE[lang + "-" + region.toUpperCase()]) return TABLE[lang + "-" + region.toUpperCase()];
    return TABLE[lang] || TABLE.en;
  }

  /* ---------------------------------------------------------- decimals --- */

  class FormatError extends Error {
    /* code is what the vectors compare and what a page translates; the
       message is an English fallback for a developer console. */
    constructor(code, message, params) {
      super(message);
      this.name = "FormatError";
      this.code = code;
      this.params = params || {};
    }
  }

  const TEN = (k) => 10n ** BigInt(k);

  /* {m, s}: the value m / 10^s, m a BigInt. A Number is read through its
     shortest decimal form (String(2.675) is "2.675"), as Kotlin reads a
     Double through toString. */
  function decimal(x) {
    if (typeof x === "bigint") return { m: x, s: 0 };
    if (typeof x !== "number" || !Number.isFinite(x)) {
      throw new FormatError("not-finite", x + " is not a finite number.", { value: x });
    }
    const p = /^(-?)(\d+)(?:\.(\d+))?(?:e([+-]\d+))?$/.exec(String(x));
    const frac = p[3] || "";
    let m = BigInt(p[2] + frac), s = frac.length - Number(p[4] || 0);
    if (s < 0) { m *= TEN(-s); s = 0; }
    return { m: p[1] ? -m : m, s };
  }

  const sign = (d) => (d.m > 0n ? 1 : d.m < 0n ? -1 : 0);
  const abs = (b) => (b < 0n ? -b : b);

  /* |d| to `decimals` places, half away from zero, as a digit string. */
  function fixed(d, decimals) {
    let q;
    if (d.s <= decimals) {
      q = abs(d.m) * TEN(decimals - d.s);
    } else {
      const div = TEN(d.s - decimals);
      q = abs(d.m) / div;
      if (2n * (abs(d.m) % div) >= div) q += 1n;
    }
    return q.toString().padStart(decimals + 1, "0");
  }

  /* Grouped only over 9999 (section 4.2), so a year or 1500 W is never split. */
  function digits(d, decimals, decimalSep, groupSep) {
    const q = fixed(d, decimals);
    let whole = decimals ? q.slice(0, -decimals) : q;
    const frac = decimals ? q.slice(-decimals) : "";
    if (groupSep != null && whole.length > 4) {
      const first = whole.length % 3 || 3;
      let out = whole.slice(0, first);
      for (let i = first; i < whole.length; i += 3) out += groupSep + whole.slice(i, i + 3);
      whole = out;
    }
    return decimals > 0 ? whole + decimalSep + frac : whole;
  }

  const UNITS = { Hz: 0, kHz: 3, MHz: 6, GHz: 9 };          // powers of ten
  const MAX_DECIMALS = { Hz: 0, kHz: 3, MHz: 6, GHz: 9 };   // down to 1 Hz
  // Section 4.1: MHz 3 by default (kHz resolution), kHz 1; GHz keeps kHz.
  const DEFAULT_DECIMALS = { Hz: 0, kHz: 1, MHz: 3, GHz: 6 };

  function checkUnit(unit) {
    if (!(unit in UNITS)) {
      throw new FormatError("bad-unit", '"' + unit + '" is not a unit these helpers know; use Hz, kHz, MHz or GHz.', { unit });
    }
  }

  function checkDecimals(decimals, unit) {
    const top = MAX_DECIMALS[unit];
    if (!Number.isInteger(decimals) || decimals < 0 || decimals > top) {
      throw new FormatError("bad-decimals", "A value in " + unit + " can show between 0 and " + top
        + " decimal places, not " + decimals + ".", { unit, decimals });
    }
  }

  const scaled = (d, unit) => ({ m: d.m, s: d.s + UNITS[unit] });
  const pageLocale = () => api.locale;

  /* frequency(14074000) is "14074.0 kHz" in English. unit "auto" is kHz
     below 30 MHz and MHz from 30 MHz up; decimals null is the unit's
     default, and trailing zeros stay so a dial does not change width as it
     tunes. Never grouped: "10,489.750" reads as 10.489 MHz in half the
     world. */
  function frequency(hz, opts) {
    const o = opts || {};
    const unit = o.unit || "auto";
    const d = decimal(hz);
    if (sign(d) < 0) {
      throw new FormatError("negative-frequency", "A frequency cannot be negative; use offset() for a shift.", { hz });
    }
    const u = unit === "auto" ? (d.m >= 30000000n * TEN(d.s) ? "MHz" : "kHz") : unit;
    checkUnit(u);
    const places = o.decimals == null ? DEFAULT_DECIMALS[u] : o.decimals;
    checkDecimals(places, u);
    return digits(scaled(d, u), places, symbols(o.locale || pageLocale()).decimal) + NBSP + u;
  }

  /* A signed shift or tone: "+1500 Hz", "−600 kHz" (U+2212 in every
     locale, section 4.1). Zero has no sign; decimals null shows as many as
     the value needs, because shifts are round numbers. */
  function offset(hz, opts) {
    const o = opts || {};
    const unit = o.unit || "Hz";
    const d = decimal(hz);
    checkUnit(unit);
    const s = scaled(d, unit);
    let places = o.decimals;
    if (places == null) {
      let m = s.m, k = s.s;
      while (k > 0 && m % 10n === 0n) { m /= 10n; k -= 1; }
      places = Math.min(Math.max(0, k), MAX_DECIMALS[unit]);
    }
    checkDecimals(places, unit);
    const text = digits(s, places, symbols(o.locale || pageLocale()).decimal);
    const zero = /^[0.,]*$/.test(text);
    return (zero ? "" : sign(d) > 0 ? "+" : MI) + text + NBSP + unit;
  }

  /* A plain number (dB, W, MER, a count) in the locale's own symbols,
     its own minus sign included. */
  function number(x, opts) {
    const o = opts || {};
    const places = o.decimals == null ? 0 : o.decimals;
    if (!Number.isInteger(places) || places < 0 || places > 12) {
      throw new FormatError("bad-decimals", "A number can show between 0 and 12 decimal places, not " + places + ".", { decimals: places });
    }
    const d = decimal(x);
    const sy = symbols(o.locale || pageLocale());
    const text = digits(d, places, sy.decimal, sy.group);
    return (sign(d) < 0 && /[1-9]/.test(text) ? sy.minus : "") + text;
  }

  /* One decimal: swr(1.5) is "1.5:1". */
  function swr(x, opts) {
    const o = opts || {};
    const d = decimal(x);
    if (d.m < TEN(d.s)) throw new FormatError("bad-swr", "An SWR cannot be below 1:1, and " + x + " is.", { value: x });
    return digits(d, 1, symbols(o.locale || pageLocale()).decimal) + ":1";
  }

  const UNIT_BY_LOWER = { hz: "Hz", khz: "kHz", mhz: "MHz", ghz: "GHz" };

  /* What a person typed, as integer Hz (a Number: every frequency we handle
     is far inside 2^53). "." or "," is the decimal separator in any locale,
     because frequencies are never grouped; spaces of every width are
     ignored; a typed unit wins over the field's (opts.unit); with neither
     the text is refused, not guessed; finer than 1 Hz is refused, not
     rounded. */
  function parseFrequency(text, opts) {
    const o = opts || {};
    const unit = o.unit == null ? null : o.unit;
    if (unit != null && unit !== "auto") checkUnit(unit);
    const field = unit === "auto" ? null : unit;
    const compact = String(text).replace(/[\s   ]/g, "");
    if (!compact) throw new FormatError("empty", "The frequency is empty; type a number such as 14074 kHz.");
    const m = /^([0-9.,]+)(\p{L}*)$/u.exec(compact);
    if (!m || !/\d/.test(m[1])) {
      throw new FormatError("not-a-number", '"' + text + '" is not a frequency.', { text });
    }
    if ((m[1].match(/[.,]/g) || []).length > 1) {
      throw new FormatError("two-separators", '"' + text + '" has more than one decimal separator.', { text });
    }
    let chosen;
    if (m[2]) {
      chosen = UNIT_BY_LOWER[m[2].toLowerCase()];
      if (!chosen) throw new FormatError("unknown-unit", '"' + m[2] + '" is not a frequency unit.', { unit: m[2] });
    } else if (field != null) {
      chosen = field;
    } else {
      throw new FormatError("no-unit", '"' + text + '" has no unit; add Hz, kHz, MHz or GHz.', { text });
    }
    const n = m[1].replace(",", ".");
    const whole = n.split(".")[0] || "0";
    const frac = n.includes(".") ? n.split(".")[1] : "";
    const k = UNITS[chosen];
    if (frac.replace(/0+$/, "").length > k) {
      throw new FormatError("too-precise", '"' + text + '" is finer than 1 Hz.', { text });
    }
    return Number(BigInt(whole) * TEN(k) + BigInt((frac + "0".repeat(k)).slice(0, k) || "0"));
  }

  /* ---------------------------------------------------------- catalog --- */

  const loaded = [];   // [primary, ..., English]: looked up in order

  function lookup(cat, key) {
    if (Object.prototype.hasOwnProperty.call(cat, key)) return cat[key];
    let node = cat;
    for (const part of key.split(".")) {
      if (node == null || typeof node !== "object" || !Object.prototype.hasOwnProperty.call(node, part)) return undefined;
      node = node[part];
    }
    return node;
  }

  function fill(text, vars) {
    return text.replace(/\{(\w+)\}/g, (all, name) => {
      if (!vars || !(name in vars)) return all;
      const v = vars[name];
      // {n} is the count a plural is chosen by, and takes the locale's
      // grouping (12 345 contacts). Nothing else is touched: a port or a
      // year is a number too and must not become "45,320", and frequencies
      // arrive already formatted by fmt.
      return name === "n" && Number.isInteger(v) ? number(v) : String(v);
    });
  }

  function t(key, vars) {
    for (const cat of loaded) {
      const entry = lookup(cat, key);
      if (typeof entry === "string") return fill(entry, vars);
      if (entry && typeof entry === "object" && "other" in entry) {
        const n = vars && typeof vars.n === "number" ? vars.n : 0;
        let cat2 = "other";
        try { cat2 = new Intl.PluralRules(api.language).select(n); } catch (e) { /* keep "other" */ }
        return fill(entry[cat2] != null ? entry[cat2] : entry.other, vars);
      }
    }
    return key;
  }

  /* data-i18n="key" replaces an element's text; data-i18n-attr=
     "title:key;aria-label:key2" sets attributes. Called once the catalog
     is in, and again by any script that builds markup from a template. */
  function apply(scope) {
    const top = scope || (typeof document !== "undefined" ? document : null);
    // Before the catalog is in, the markup's own English stays: replacing
    // it with keys would be worse than leaving it. load() applies again.
    if (!top || !top.querySelectorAll || !loaded.length) return;
    top.querySelectorAll("[data-i18n]").forEach((el) => { el.textContent = t(el.getAttribute("data-i18n")); });
    top.querySelectorAll("[data-i18n-attr]").forEach((el) => {
      el.getAttribute("data-i18n-attr").split(";").forEach((pair) => {
        const at = pair.indexOf(":");
        if (at > 0) el.setAttribute(pair.slice(0, at).trim(), t(pair.slice(at + 1).trim()));
      });
    });
  }

  function wanted() {
    const out = [];
    try {
      const q = new URLSearchParams(root.location ? root.location.search : "").get("lang");
      if (q) out.push(q);
    } catch (e) { /* no location: not a page */ }
    try {
      const saved = root.localStorage && root.localStorage.getItem("shack.lang");
      if (saved) out.push(saved);
    } catch (e) { /* storage blocked: the browser's languages decide */ }
    const nav = root.navigator || {};
    (nav.languages && nav.languages.length ? nav.languages : [nav.language || "en"]).forEach((l) => out.push(l));
    return out;
  }

  // Resolved against this script's own address, so the catalogs are found
  // under whatever prefix the app is published at (never an absolute URL).
  const here = root.document && root.document.currentScript ? root.document.currentScript.src : null;

  async function fetchCatalog(tag) {
    if (!here || typeof fetch !== "function") return null;
    try {
      const res = await fetch(new URL("i18n/" + tag + ".json", here), { credentials: "same-origin" });
      return res.ok ? await res.json() : null;
    } catch (e) {
      return null;
    }
  }

  /* Load the first wanted language that has a catalog (exact tag, then its
     language), with English underneath. Resolves with the language chosen. */
  async function load(languages) {
    const english = await fetchCatalog("en");
    let primary = null, chosen = "en";
    for (const want of languages || wanted()) {
      const tag = String(want).replace(/_/g, "-");
      const base = tag.split("-")[0].toLowerCase();
      if (base === "en" && tag.toLowerCase() !== "en-xa") break;
      for (const candidate of tag === base ? [tag] : [tag, base]) {
        primary = await fetchCatalog(candidate);
        if (primary) { chosen = candidate; break; }
      }
      if (primary) break;
    }
    loaded.length = 0;
    if (primary) loaded.push(primary);
    if (english) loaded.push(english);
    api.language = chosen;
    if (root.document && root.document.documentElement) root.document.documentElement.lang = chosen;
    apply();
    return chosen;
  }

  /* Tests and node: put catalogs in directly, primary first. */
  function use(catalogs, language) {
    loaded.length = 0;
    catalogs.forEach((c) => loaded.push(c));
    api.language = language || "en";
  }

  const nav = root.navigator || {};
  const api = {
    t, apply, load, use,
    fmt: { frequency, offset, number, swr, parseFrequency, FormatError, symbols },
    // Numbers follow the browser's locale even while the words are English:
    // a German operator reads 145,500 MHz from the first beta.
    locale: (nav.languages && nav.languages[0]) || nav.language || "en",
    language: "en",
    ready: null,
  };
  api.ready = root.document ? load() : Promise.resolve("en");
  root.ShackI18n = api;
  // A property rather than a declaration, so a page script with a top-level
  // `let t` of its own is not a SyntaxError.
  root.t = t;
  if (typeof module === "object" && module.exports) module.exports = api;
})(typeof self !== "undefined" ? self : globalThis);
