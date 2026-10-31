/* The pages service's script inside a page (retinue/pagesd.py `inject`): the first script of a page whose own
   scripts run, put there by the service with the page's saved values in place of DATA and whether they are kept in
   place of KEEP. The page's document has an opaque origin (the CSP `sandbox`): the browser gives it no
   localStorage, no sessionStorage, no cookies and no network. This gives it back what it lost, kept by the shell:
   - `localStorage` and `sessionStorage`, synchronous, read from the values the page was served with; every change is
     sent to the shell, which saves it for this page alone (sessionStorage is not saved: it lives for this visit);
   - `window.storage`, the shape of Claude's artifacts' storage (get/set/delete/list, each a promise), so a page made
     in claude.ai with persistent storage works as it is;
   - links: a click on a link to another page or another site, and `window.open`, go to the shell, which opens them
     (the page's frame may not navigate itself anywhere else, nor open a window).
   The channel to the shell is a MessageChannel port the shell posts to this frame once it has loaded; until then the
   changes wait in order. The values are the page's own: nothing here can name another page. */
(function (DATA, KEEP) {
  "use strict";
  var values = Object.create(null);
  Object.keys(DATA).forEach(function (key) { values[key] = String(DATA[key]); });
  var port = null;
  var waiting = [];

  function send(op) {
    if (port) port.postMessage(op); else waiting.push(op);
  }

  addEventListener("message", function (event) {
    if (port || event.source !== parent || event.data !== "retinue-pages:port" || !event.ports || !event.ports[0]) return;
    port = event.ports[0];
    waiting.splice(0).forEach(function (op) { port.postMessage(op); });
  });

  function set(key, value) {
    value = String(value);
    if (values[key] === value) return;
    values[key] = value;
    if (KEEP) send({ op: "set", key: key, value: value });
  }
  function remove(key) {
    if (!(key in values)) return;
    delete values[key];
    if (KEEP) send({ op: "del", key: key });
  }

  // A Storage: getItem/setItem/removeItem/clear/key/length, and its keys as properties, as a browser's has them.
  function storage(prefix, kept) {
    var own = Object.create(null);
    function keys() {
      return kept ? Object.keys(values).filter(function (k) { return k.indexOf(prefix) === 0; })
        .map(function (k) { return k.slice(prefix.length); }) : Object.keys(own);
    }
    var api = {
      getItem: function (key) {
        key = String(key);
        if (!kept) return key in own ? own[key] : null;
        return (prefix + key) in values ? values[prefix + key] : null;
      },
      setItem: function (key, value) {
        if (kept) set(prefix + String(key), value); else own[String(key)] = String(value);
      },
      removeItem: function (key) {
        if (kept) remove(prefix + String(key)); else delete own[String(key)];
      },
      clear: function () {
        keys().forEach(function (key) { api.removeItem(key); });
      },
      key: function (n) { var all = keys(); return n >= 0 && n < all.length ? all[n] : null; },
    };
    return new Proxy(api, {
      get: function (target, name) {
        if (name === "length") return keys().length;
        if (typeof name !== "string" || name in target) return target[name];
        var found = target.getItem(name);
        return found === null ? undefined : found;
      },
      set: function (target, name, value) { target.setItem(name, value); return true; },
      deleteProperty: function (target, name) { target.removeItem(name); return true; },
      has: function (target, name) { return name in target || target.getItem(name) !== null; },
      ownKeys: function () { return keys(); },
      getOwnPropertyDescriptor: function (target, name) {
        var found = target.getItem(name);
        return found === null ? undefined : { value: found, writable: true, enumerable: true, configurable: true };
      },
    });
  }

  function define(name, value) {
    try { Object.defineProperty(window, name, { value: value, configurable: true, enumerable: true, writable: false }); }
    catch (e) { /* a browser that will not let it be replaced: the page catches the error it always caught */ }
  }
  define("localStorage", storage("ls:", true));
  define("sessionStorage", storage("ss:", false));

  // Claude artifacts' storage: personal and shared are two names here, both this page's own.
  function scope(shared) { return shared ? "sh:" : "st:"; }
  define("storage", {
    get: function (key, shared) {
      var full = scope(shared) + String(key);
      return Promise.resolve(full in values ? { key: String(key), value: values[full], shared: !!shared } : null);
    },
    set: function (key, value, shared) {
      set(scope(shared) + String(key), value);
      return Promise.resolve({ key: String(key), value: String(value), shared: !!shared });
    },
    delete: function (key, shared) {
      remove(scope(shared) + String(key));
      return Promise.resolve({ key: String(key), deleted: true, shared: !!shared });
    },
    list: function (prefix, shared) {
      var start = scope(shared) + (prefix ? String(prefix) : "");
      var keys = Object.keys(values).filter(function (k) { return k.indexOf(start) === 0; })
        .map(function (k) { return k.slice(3); });
      return Promise.resolve({ keys: keys, prefix: prefix || "", shared: !!shared });
    },
  });

  // Links: the shell opens them. A link inside the page (#…) stays the page's; a click the page handled is its own.
  function open(href) {
    send({ op: "open", href: String(href) });
    return null;
  }
  define("open", function (url) { return open(new URL(String(url), document.baseURI).href); });
  addEventListener("click", function (event) {
    if (event.defaultPrevented || event.button !== 0) return;
    var link = event.target && event.target.closest ? event.target.closest("a[href], area[href]") : null;
    if (!link) return;
    var href = link.getAttribute("href") || "";
    if (href.charAt(0) === "#" || /^\s*javascript:/i.test(href) || link.hasAttribute("download")) return;
    event.preventDefault();
    open(link.href);
  });
})(DATA, KEEP);
