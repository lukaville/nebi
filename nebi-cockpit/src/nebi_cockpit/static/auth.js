// Access token plumbing for `nebi-cockpit serve --host <non-loopback>`.
// The server prints `http://<host>:<port>/?token=...`; this script keeps that token for the tab
// (sessionStorage), strips it from the address bar, and attaches it to every same-origin request:
// fetch -> `X-Pynebi-Token` header, WebSocket -> `?token=`, <img>/<video>/downloads -> SameSite=Strict cookie.
// Loopback-only servers need no token and this script then does nothing.
(() => {
  "use strict";
  const KEY = "pynebi_token";
  let tok = null;
  try {
    const u = new URL(location.href);
    tok = u.searchParams.get("token");
    if (tok) {
      sessionStorage.setItem(KEY, tok);
      u.searchParams.delete("token");
      history.replaceState(null, "", u.pathname + u.search + u.hash);
    } else {
      tok = sessionStorage.getItem(KEY);
    }
  } catch (_) {}
  if (!tok) return;

  document.cookie = `${KEY}=${encodeURIComponent(tok)}; path=/; SameSite=Strict`;

  const sameOrigin = (url) => {
    try {
      return new URL(url, location.href).origin === location.origin;
    } catch (_) {
      return false;
    }
  };

  const origFetch = window.fetch.bind(window);
  window.fetch = (input, init = {}) => {
    const url = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
    if (sameOrigin(url)) {
      const headers = new Headers(init.headers || (input instanceof Request ? input.headers : undefined));
      headers.set("X-Pynebi-Token", tok);
      init = { ...init, headers };
    }
    return origFetch(input, init);
  };

  const OrigWS = window.WebSocket;
  window.WebSocket = class extends OrigWS {
    constructor(url, protocols) {
      const u = new URL(url, location.href);
      if (u.host === location.host) u.searchParams.set("token", tok);
      super(u.href, protocols);
    }
  };
})();
