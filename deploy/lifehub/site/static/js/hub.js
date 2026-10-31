// The life hub's one script: relative times and the glasses theme. Nothing else, nothing fetched. Every element it
// touches carries its text already (built by Hugo), so with this script blocked the page says the same in clock time.
(function () {
  "use strict";
  var root = document.documentElement;
  var KEY = "lifehub-theme";

  // --- the glasses theme: pure black, borders only, bigger type; remembered on this device ----------------------
  function stored() {
    try { return window.localStorage.getItem(KEY); } catch (e) { return null; }
  }
  function store(value) {
    try {
      if (value) { window.localStorage.setItem(KEY, value); } else { window.localStorage.removeItem(KEY); }
    } catch (e) { /* private mode: the theme lasts this page only */ }
  }
  function apply(value) {
    if (value === "glasses") { root.setAttribute("data-theme", "glasses"); } else { root.removeAttribute("data-theme"); }
    var buttons = document.querySelectorAll("[data-glasses]");
    for (var i = 0; i < buttons.length; i++) { buttons[i].setAttribute("aria-pressed", String(value === "glasses")); }
  }
  apply(stored());
  var toggles = document.querySelectorAll("[data-glasses]");
  for (var t = 0; t < toggles.length; t++) {
    toggles[t].hidden = false;
    toggles[t].addEventListener("click", function () {
      var next = root.getAttribute("data-theme") === "glasses" ? "" : "glasses";
      store(next);
      apply(next);
    });
  }

  // --- relative times -----------------------------------------------------------------------------------------
  function span(seconds) {
    var minutes = Math.max(1, Math.round(seconds / 60));
    if (minutes < 60) { return minutes + " min"; }
    var hours = Math.floor(minutes / 60), rest = minutes % 60;
    if (hours < 24) { return rest && hours < 6 ? hours + " h " + rest + " min" : hours + " h"; }
    var days = Math.floor(hours / 24), left = hours % 24;
    return left && days < 3 ? days + " d " + left + " h" : days + " d";
  }
  function ago(seconds) {
    return seconds < 60 ? "just now" : span(seconds) + " ago";
  }
  function tick() {
    var now = Date.now() / 1000, i, el, at;
    // data-ago="<unix>": «12 min ago»; with data-stale="<seconds>" it turns into a warning past that age.
    var past = document.querySelectorAll("[data-ago]");
    for (i = 0; i < past.length; i++) {
      el = past[i];
      at = Number(el.getAttribute("data-ago"));
      if (!at) { continue; }
      el.textContent = (el.getAttribute("data-prefix") || "") + ago(now - at);
      if (el.hasAttribute("data-stale")) {
        var old = now - at > Number(el.getAttribute("data-stale"));
        el.classList.toggle("stale", old);
        if (old) { el.textContent = "data out of date · " + ago(now - at); }
      }
    }
    // data-in="<unix start>" data-end="<unix end>": «in 42 min», «on now · 20 min left», «ended».
    var ahead = document.querySelectorAll("[data-in]");
    for (i = 0; i < ahead.length; i++) {
      el = ahead[i];
      at = Number(el.getAttribute("data-in"));
      var end = Number(el.getAttribute("data-end")) || at;
      if (!at) { continue; }
      if (now < at) { el.textContent = "in " + span(at - now); }
      else if (now < end) { el.textContent = "on now · " + span(end - now) + " left"; }
      else { el.textContent = "ended"; }
    }
  }
  tick();
  window.setInterval(tick, 30000);
})();
