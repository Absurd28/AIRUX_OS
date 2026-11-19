// NEOX privacy, in the page's own JavaScript world, before any page script runs.
//  - Global Privacy Control: navigator.globalPrivacyControl = true (the Sec-GPC header is set by a rule).
//  - Fingerprinting protection ("farbling", like Brave): canvas and audio readouts get tiny noise that
//    is stable for a site for one day but differs between sites, so the same device looks different
//    to different sites; hardwareConcurrency is reported as a common value. Partial protection: it
//    does not cover every fingerprinting technique.
(() => {
  "use strict";
  try {
    Object.defineProperty(Navigator.prototype, "globalPrivacyControl", { get: () => true, configurable: true });
  } catch (_) {}

  // per-site, per-day key: different sites cannot link the same canvas or audio fingerprint, while
  // one site always sees the same values (so its pages work and repeated reads agree)
  const day = new Date().toISOString().slice(0, 10);
  let key = 2166136261;
  for (const ch of (location.origin + "|" + day)) { key ^= ch.charCodeAt(0); key = Math.imul(key, 16777619) >>> 0; }
  // a stateless hash of (key, position): the same position always gets the same noise
  const noise = (i) => {
    let h = (key ^ Math.imul(i + 1, 2654435761)) >>> 0;
    h ^= h >>> 16; h = Math.imul(h, 2246822507) >>> 0; h ^= h >>> 13; h = Math.imul(h, 3266489909) >>> 0; h ^= h >>> 16;
    return h / 4294967296;
  };
  const nudged = new WeakSet();

  function farbleImageData(data) {
    // change the lowest bit of about 1 in 50 pixels: invisible, but it changes the hash
    for (let i = 0; i < data.length; i += 4) {
      if (noise(i) < 0.02) data[i] ^= 1;
    }
  }

  function farbleCanvas(canvas) {
    if (nudged.has(canvas) || !canvas.width || !canvas.height) return;
    try {
      const ctx = canvas.getContext("2d");
      if (!ctx) return;
      const w = Math.min(canvas.width, 64), h = Math.min(canvas.height, 64);
      const img = origGetImageData.call(ctx, 0, 0, w, h);
      farbleImageData(img.data);
      ctx.putImageData(img, 0, 0);
      nudged.add(canvas);
    } catch (_) { /* webgl or tainted canvas: leave it */ }
  }

  const C2D = CanvasRenderingContext2D.prototype;
  const origGetImageData = C2D.getImageData;
  C2D.getImageData = function (...args) {
    const img = origGetImageData.apply(this, args);
    farbleImageData(img.data);
    return img;
  };
  const HC = HTMLCanvasElement.prototype;
  for (const name of ["toDataURL", "toBlob"]) {
    const orig = HC[name];
    HC[name] = function (...args) { farbleCanvas(this); return orig.apply(this, args); };
  }

  if (window.AudioBuffer) {
    const origGet = AudioBuffer.prototype.getChannelData;
    const done = new WeakSet();
    AudioBuffer.prototype.getChannelData = function (...args) {
      const data = origGet.apply(this, args);
      if (!done.has(data)) {
        for (let i = 0; i < data.length; i += 97) data[i] += (noise(i) - 0.5) * 1e-7;
        done.add(data);
      }
      return data;
    };
  }

  try {
    Object.defineProperty(Navigator.prototype, "hardwareConcurrency", { get: () => 4, configurable: true });
  } catch (_) {}
})();
