'use strict';
(() => {
  const $ = id => document.getElementById(id);
  for (const [button, view] of [['show-statistics','statistics'],['show-archive','archive']]) {
    $(button).addEventListener('click',() => {
      $('statistics-view').hidden = view !== 'statistics';
      $('archive-view').hidden = view !== 'archive';
      $('show-statistics').setAttribute('aria-pressed',String(view === 'statistics'));
      $('show-archive').setAttribute('aria-pressed',String(view === 'archive'));
      document.dispatchEvent(new CustomEvent('admin:view',{detail:view}));
    });
  }
})();
