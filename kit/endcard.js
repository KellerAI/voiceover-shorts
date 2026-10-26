/*
 * KAEndCard: shared Keller AI brand end card for HyperFrames shorts compositions.
 *
 * The card enters by a HARD CUT (no fade) on an opaque brand background and stays
 * to the end of the composition, so preflight/endcard.py finds it with its
 * default scene method and sees no near-black frame in the hold.
 *
 * Template contract
 *   1. Copy into the project (HyperFrames rejects paths above the project root):
 *        <plugin>/kit/endcard.js                -> <project>/kit/endcard.js
 *        <plugin>/kit/brand/keller-ai-logo.svg  -> <project>/assets/brand/keller-ai-logo.svg
 *        <plugin>/kit/fonts/LilitaOne.woff2     -> <project>/assets/fonts/LilitaOne.woff2
 *   2. Put this @font-face INLINE in the page <style> (HyperFrames only skips its
 *      Google Fonts fetch for families with an inline @font-face):
 *        @font-face { font-family: "KS Lilita"; font-weight: 400 900;
 *                     src: url("assets/fonts/LilitaOne.woff2") format("woff2"); }
 *      KAEndCard.fontFaceCSS() returns that exact text.
 *   3. Timing: end = KAEndCard.plan({ lastWordEnd: W }).end. Write it into the root
 *      data-duration (HyperFrames reads it from the HTML, not from JS) and end the
 *      timeline there: tl.set({}, {}, end).
 *   4. After building the paused GSAP timeline:
 *        KAEndCard.mount({ tl: tl, root: document.getElementById("root"),
 *                          start: plan.start, brand: { line: "..." } });
 *
 * API
 *   KAEndCard.DEFAULT_HOLD = 2.0   seconds after max(start, lastWordEnd); preflight/endcard.py needs >= 1.5
 *   KAEndCard.DEFAULT_GAP  = 0.1   seconds from lastWordEnd to the cut when start is not given
 *   KAEndCard.plan({ lastWordEnd, start?, hold?, gap?, fps? }) -> { start, end, hold }
 *       end is rounded UP to a whole frame at fps (default 30). Throws if start is
 *       more than 0.05 s before lastWordEnd (the card would cover the last word) or
 *       hold < 1.5 (pass allowShortHold: true only to build a planted negative).
 *   KAEndCard.mount({ tl, root, start, brand?, fontFamily? }) -> HTMLElement
 *       Appends a full-frame absolute child to root; a static root is made position:relative.
 *       brand: { logo, name, line, bg, fg, accent } (defaults in DEFAULT_BRAND,
 *       colours from the official brand manifest: theme #10243E, background #FBF7F0).
 *   KAEndCard.fontFaceCSS(base = "assets/fonts/") -> string
 *
 * Works in the browser (window.KAEndCard) and in Node (module.exports) for plan().
 */
(function (global) {
  "use strict";

  var DEFAULT_HOLD = 2.0;
  var DEFAULT_GAP = 0.1;
  var MIN_HOLD = 1.5;
  var COVER_TOL = 0.05;

  var DEFAULT_BRAND = {
    logo: "assets/brand/keller-ai-logo.svg",
    name: "KELLER AI",
    line: "",
    bg: "#10243E",
    fg: "#FBF7F0",
    accent: "#8B9090"
  };

  function nil(v) {
    return v === null || v === undefined;
  }

  function num(v, name) {
    var n = Number(v);
    if (!isFinite(n)) throw new Error("KAEndCard: " + name + " must be a finite number");
    return n;
  }

  function plan(o) {
    o = o || {};
    var lwe = num(o.lastWordEnd, "lastWordEnd");
    var hold = nil(o.hold) ? DEFAULT_HOLD : num(o.hold, "hold");
    var gap = nil(o.gap) ? DEFAULT_GAP : num(o.gap, "gap");
    var fps = nil(o.fps) ? 30 : num(o.fps, "fps");
    var allowShort = o.allowShortHold === true;
    if (hold < MIN_HOLD && !allowShort) {
      throw new Error("KAEndCard: hold " + hold + " s is under the " + MIN_HOLD + " s minimum");
    }
    var start = nil(o.start) ? lwe + gap : num(o.start, "start");
    if (start < lwe - COVER_TOL) {
      throw new Error("KAEndCard: start " + start + " covers the last word (ends " + lwe + ")");
    }
    var end = Math.max(start, lwe) + hold;
    end = Math.ceil(end * fps - 1e-6) / fps;
    return { start: start, end: end, hold: hold };
  }

  function fontFaceCSS(base) {
    base = nil(base) ? "assets/fonts/" : base;
    return '@font-face { font-family: "KS Lilita"; font-weight: 400 900; src: url("' +
      base + 'LilitaOne.woff2") format("woff2"); }';
  }

  function merge(a, b) {
    var out = {}, k;
    for (k in a) out[k] = a[k];
    if (b) for (k in b) if (!nil(b[k])) out[k] = b[k];
    return out;
  }

  function mount(o) {
    if (!o || !o.tl || !o.root) throw new Error("KAEndCard.mount needs { tl, root, start }");
    var start = num(o.start, "start");
    var brand = merge(DEFAULT_BRAND, o.brand);
    var family = o.fontFamily || '"KS Lilita"';
    var doc = o.root.ownerDocument;

    var el = doc.createElement("div");
    el.className = "ka-endcard";
    el.setAttribute("data-ka-endcard", String(start));
    el.style.cssText = "position:absolute;left:0;top:0;width:100%;height:100%;z-index:9000;" +
      "display:flex;flex-direction:column;align-items:center;justify-content:center;" +
      "background:" + brand.bg + ";color:" + brand.fg + ";visibility:hidden;opacity:0;";

    var logo = doc.createElement("img");
    logo.className = "ka-endcard-logo";
    logo.src = brand.logo;
    logo.alt = "Keller AI";
    logo.style.cssText = "width:34%;height:auto;border-radius:6%;" +
      "box-shadow:0 0 0 6px " + brand.accent + ";";
    el.appendChild(logo);

    var name = doc.createElement("div");
    name.className = "ka-endcard-name";
    name.textContent = brand.name;
    name.style.cssText = "margin-top:72px;font-family:" + family + ";font-size:150px;" +
      "letter-spacing:4px;line-height:1;";
    el.appendChild(name);

    if (brand.line) {
      var line = doc.createElement("div");
      line.className = "ka-endcard-line";
      line.textContent = brand.line;
      line.style.cssText = "margin-top:36px;font-family:" + family + ";font-size:64px;" +
        "color:" + brand.accent + ";max-width:85%;text-align:center;line-height:1.15;";
      el.appendChild(line);
    }

    var view = doc.defaultView;
    if (view && view.getComputedStyle(o.root).position === "static") o.root.style.position = "relative";
    o.root.appendChild(el);
    // Hard cut in: autoAlpha sets visibility + opacity at one instant.
    o.tl.set(el, { autoAlpha: 1 }, start);
    // Small settle on the logo after the cut; the background never changes.
    o.tl.fromTo(logo, { scale: 0.92 }, { scale: 1, duration: 0.4, ease: "power2.out" }, start);
    return el;
  }

  var api = {
    DEFAULT_HOLD: DEFAULT_HOLD,
    DEFAULT_GAP: DEFAULT_GAP,
    MIN_HOLD: MIN_HOLD,
    DEFAULT_BRAND: DEFAULT_BRAND,
    plan: plan,
    mount: mount,
    fontFaceCSS: fontFaceCSS
  };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  if (global) global.KAEndCard = api;
})(typeof window !== "undefined" ? window : null);
