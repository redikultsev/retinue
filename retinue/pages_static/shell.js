/* The shell around one page (retinue/pagesd.py): the page runs in the frame #page on an opaque origin, this script
   runs on the service's own. It is the page's only way out, and every way is this page's alone — the page's id is
   the shell's, from its own address, never from a message:
   - the page's changes to its storage come over a MessageChannel port given to the frame at each load, are batched
     and saved (`PUT /api/store/<id>`), and the bar says whether they were (owner only; a visitor's stay in his frame);
   - a link the page asks to open: another page of this site in this window, anything else in a new one, never with
     the page's address as the referrer;
   - the owner's «Share»: on which public site and whether as its home page (`/api/share/<id>`). */
(function () {
  "use strict";
  var body = document.body;
  var page = body.dataset.page;
  var owner = body.dataset.mode === "owner";
  var frame = document.getElementById("page");
  var status = document.getElementById("saved");
  var pending = Object.create(null);
  var timer = 0;
  var saving = false;
  var failures = 0;

  function say(text, bad) {
    if (!status) return;
    status.textContent = text;
    status.classList.toggle("bad", !!bad);
  }

  function api(method, path, data, keepalive) {
    return fetch(path, {
      method: method, keepalive: !!keepalive, credentials: "omit", cache: "no-store",
      headers: { "Content-Type": "application/json", "X-Pages": "1" },
      body: data === undefined ? undefined : JSON.stringify(data),
    }).then(function (response) {
      return response.json().catch(function () { return {}; }).then(function (answer) {
        if (!response.ok) throw new Error(answer.error || "the service answered " + response.status);
        return answer;
      });
    });
  }

  function ops() {
    var out = Object.keys(pending).map(function (key) {
      return pending[key] === null ? { op: "del", key: key } : { op: "set", key: key, value: pending[key] };
    });
    pending = Object.create(null);
    return out;
  }

  function save(keepalive) {
    clearTimeout(timer);
    timer = 0;
    if (saving && !keepalive) { timer = setTimeout(save, 400); return; }
    var batch = ops();
    if (!batch.length) return;
    saving = true;
    say("Saving…");
    api("PUT", "/api/store/" + page, { ops: batch }, keepalive).then(function () {
      failures = 0;
      say("Saved");
    }, function (error) {
      batch.forEach(function (op) {  // put back what did not go, unless the page changed it since
        if (!(op.key in pending)) pending[op.key] = op.op === "del" ? null : op.value;
      });
      failures += 1;
      say("Not saved: " + error.message, true);
      if (failures < 6) timer = setTimeout(save, Math.min(30000, 1000 * Math.pow(2, failures)));
    }).then(function () { saving = false; });
  }

  function opened(href) {
    var url;
    try { url = new URL(href, location.href); } catch (e) { return; }
    if (url.origin === location.origin) {  // another page of this site: here, by its own address
      var path = url.pathname.replace(/^\/raw\//, "/");
      location.assign(path + url.search + url.hash);
      return;
    }
    if (!/^(https?|mailto|tel):$/.test(url.protocol)) return;
    if (!window.open(url.href, "_blank", "noopener,noreferrer")) {  // a blocked window: a link to tap instead
      var link = document.getElementById("blocked");
      if (link) { link.href = url.href; link.textContent = "Open " + url.host; link.hidden = false; }
    }
  }

  function listen(event) {
    var op = event.data || {};
    if (op.op === "open" && typeof op.href === "string") return opened(op.href);
    if (!owner || typeof op.key !== "string") return;
    if (op.op === "set" && typeof op.value === "string") pending[op.key] = op.value;
    else if (op.op === "del") pending[op.key] = null;
    else return;
    if (!timer) timer = setTimeout(save, 400);
  }

  var channel = null;
  frame.addEventListener("load", function () {
    if (channel) channel.port1.close();
    channel = new MessageChannel();
    channel.port1.onmessage = listen;
    frame.contentWindow.postMessage("retinue-pages:port", "*", [channel.port2]);
  });
  addEventListener("pagehide", function () { if (owner) save(true); });

  // --- the owner's «Share» ---------------------------------------------------------------------------------------
  var toggle = document.getElementById("share");
  var panel = document.getElementById("sharing");
  if (!owner || !toggle || !panel) return;
  var list = panel.querySelector("[data-list]");
  var form = panel.querySelector("form");
  var hosts = form.querySelector("select");
  var home = form.querySelector("input[type=checkbox]");
  var said = panel.querySelector("[data-said]");

  function row(item) {
    var li = document.createElement("li");
    var link = document.createElement("a");
    link.href = item.url;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    link.textContent = item.url.replace(/^https:\/\//, "");
    var copy = document.createElement("button");
    copy.type = "button";
    copy.textContent = "Copy";
    copy.addEventListener("click", function () {
      navigator.clipboard.writeText(item.url).then(function () { copy.textContent = "Copied"; },
        function () { copy.textContent = "Copy failed"; });
    });
    var stop = document.createElement("button");
    stop.type = "button";
    stop.textContent = "Stop sharing";
    stop.className = "quiet";
    stop.addEventListener("click", function () {
      api("DELETE", "/api/share/" + page, { host: item.host, home: item.home }).then(show, fail);
    });
    li.append(link, copy, stop);
    return li;
  }

  function show(state) {
    said.textContent = "";
    list.replaceChildren.apply(list, (state.shared || []).map(row));
    list.hidden = !(state.shared || []).length;
    toggle.textContent = (state.shared || []).length ? "Shared" : "Share";
    toggle.classList.toggle("on", !!(state.shared || []).length);
    if (!hosts.options.length) {
      (state.hosts || []).forEach(function (host) { hosts.add(new Option(host, host)); });
    }
    form.hidden = !(state.hosts || []).length;
    if (!(state.hosts || []).length) said.textContent = "No public site is set up (docs/pages.md).";
  }
  function fail(error) { said.textContent = error.message; }

  toggle.addEventListener("click", function () {
    var open = panel.hidden;
    panel.hidden = !open;
    toggle.setAttribute("aria-expanded", String(open));
    if (open) api("GET", "/api/share/" + page).then(show, fail);
  });
  form.addEventListener("submit", function (event) {
    event.preventDefault();
    api("PUT", "/api/share/" + page, { host: hosts.value, home: home.checked }).then(show, fail);
  });
  api("GET", "/api/share/" + page).then(show, function () {});
})();
