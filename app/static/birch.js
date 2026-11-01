'use strict';
// The trunk is one tall tile of the birch bark. Its height on the page is rarely a whole number of tiles,
// so the tile is stretched a little to fit a whole number. The crown and the roots then meet the trunk exactly.
// Without this script the tile stays at its natural size and meets the roots, and a small step may show under the crown.
(() => {
  const trunk = document.querySelector('.birch-trunk');
  if (!trunk) return;
  const fit = () => {
    const width = trunk.offsetWidth, height = trunk.offsetHeight;
    if (!width || !height) return;
    const tile = width * 3; // the tile is 400 by 1200 units
    const count = Math.max(1, Math.round(height / tile));
    trunk.style.backgroundSize = `100% ${(height / count).toFixed(2)}px`;
  };
  fit();
  if (typeof ResizeObserver === 'function') new ResizeObserver(fit).observe(trunk);
  else window.addEventListener('resize', fit);
})();
