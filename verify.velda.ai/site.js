(function () {
  var f = document.getElementById("f");
  if (!f) return;
  var oth = document.getElementById("oth");
  var ot = document.getElementById("othtxt");
  oth.addEventListener("change", function () {
    ot.style.display = oth.checked ? "block" : "none";
  });

  function mark(el, ok) {
    el.classList.toggle("bad", !ok);
    return ok;
  }

  function looksLikeSecret(text) {
    return /-----BEGIN [A-Z ]*PRIVATE KEY-----/.test(text) ||
      /\bsk_(live|test)_[A-Za-z0-9]/.test(text) ||
      /\bAIza[0-9A-Za-z\-_]{20,}/.test(text) ||
      /\bya29\.[0-9A-Za-z\-_]+/.test(text) ||
      /\b[0-9]{13,19}\b/.test(text);
  }

  window.validateIntake = function (form) {
    var ok = true;
    var first = null;
    function bad(el) {
      ok = false;
      first = first || el;
      mark(el, false);
    }
    form.querySelectorAll("[data-req]").forEach(function (d) {
      var input = d.querySelector("input");
      var value = input.value.trim();
      var good = value.length > 0;
      if (input.type === "email") good = /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(value);
      if (input.type === "url") good = /^https:\/\/[^\s]+\.[^\s]+$/.test(value);
      if (input.name === "project_id") good = /^[a-z][a-z0-9-]{4,28}[a-z0-9]$/.test(value);
      if (!mark(d, good)) bad(d);
    });
    var data = document.getElementById("data");
    var dataOk = !!form.querySelector("[name=google_data]:checked");
    if (oth.checked && !ot.value.trim()) dataOk = false;
    if (!mark(data, dataOk)) bad(data);
    ["plan", "access", "agree"].forEach(function (id) {
      var box = document.getElementById(id);
      var sel = id === "plan" ? "[name=plan]:checked" : id === "access" ? "[name=added_access]:checked" : "[name=agree_terms]:checked";
      if (!mark(box, !!form.querySelector(sel))) bad(box);
    });
    var notes = document.getElementById("notes-wrap");
    var notesOk = !looksLikeSecret(document.getElementById("notes").value);
    if (!mark(notes, notesOk)) bad(notes);
    return { ok: ok, first: first };
  };

  f.addEventListener("submit", function (e) {
    e.preventDefault();
    var result = window.validateIntake(f);
    if (!result.ok) {
      result.first.scrollIntoView({ behavior: "smooth", block: "center" });
      var focus = result.first.querySelector("input,textarea");
      if (focus) focus.focus();
      return;
    }
    var thanks = document.getElementById("thanks");
    function done() {
      f.style.display = "none";
      thanks.style.display = "block";
      window.scrollTo(0, 0);
    }
    var local = location.hostname === "127.0.0.1" || location.hostname === "localhost";
    if (local && /(?:\?|&)nosend=1(?:&|$)/.test(location.search)) {
      thanks.querySelector("h2").textContent = "Form checks passed. Nothing was sent.";
      done();
      return;
    }
    if (f.action.indexOf("{{") !== -1) {
      done();
      return;
    }
    fetch(f.action, {
      method: "POST",
      body: new FormData(f),
      headers: { Accept: "application/json" }
    }).then(function (r) {
      if (r.ok) done();
      else window.alert("Sorry, the form did not send. Please email getappverified@gmail.com.");
    }).catch(function () {
      window.alert("Sorry, the form did not send. Check your connection and try again, or email getappverified@gmail.com.");
    });
  });
})();
