/* A short-lived native-app pairing page only. Never inspect account pages. */
(() => {
  if (window.top !== window || location.protocol !== 'http:' || location.hostname !== '127.0.0.1' ||
      !location.port || !/^\/rf-connect\/[A-Za-z0-9_-]{43}$/.test(location.pathname) || location.search || location.hash) return;
  const address = document.documentElement.getAttribute('data-rf-pair');
  if (!address) return;
  chrome.runtime.sendMessage({action: 'auto_pair', address}).then(result => {
    if (result?.accepted) document.documentElement.setAttribute('data-rf-extension', 'connected');
  }).catch(() => {});
})();
