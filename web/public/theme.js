// Apply the saved theme before first paint to avoid a flash.
(function () {
  var saved = null;
  try { saved = localStorage.getItem('ictl-theme'); } catch (e) {}
  var dark = saved === 'dark' || (saved !== 'light' && matchMedia('(prefers-color-scheme: dark)').matches);
  document.documentElement.dataset.theme = dark ? 'dark' : 'light';
})();
