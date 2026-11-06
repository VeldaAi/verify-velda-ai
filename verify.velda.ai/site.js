(function () {
  var f = document.getElementById("f");
  if (!f) return;
  var oth = document.getElementById("oth");
  var ot = document.getElementById("othtxt");
  var stage2 = document.getElementById("stage2-wrap");
  var track = document.getElementById("verification_track");
  var SENSITIVE = {
    "Google Drive": 1,
    "Gmail": 1,
    "Calendar": 1,
    "Sheets/Docs": 1,
    "Other": 1
  };
  var SECRET_MSG = "Take out passwords, card numbers, API keys, tokens, and client secrets. Email those to nobody.";

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

  function validEmail(value) {
    return /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(value);
  }

  function validHttps(value) {
    return /^https:\/\/[^\s]+\.[^\s]+$/.test(value);
  }

  function stage2On() {
    var on = false;
    f.querySelectorAll("[name=google_data]:checked").forEach(function (box) {
      if (SENSITIVE[box.value]) on = true;
    });
    return on;
  }

  function syncStage2() {
    var on = stage2On();
    if (stage2) {
      stage2.hidden = !on;
      stage2.querySelectorAll("input, textarea").forEach(function (el) {
        el.disabled = !on;
      });
      if (!on) {
        ["picker", "scope-wrap", "demo-wrap"].forEach(function (id) {
          var el = document.getElementById(id);
          if (el) el.classList.toggle("bad", false);
        });
      }
    }
    if (track) {
      track.value = on ? "Stage 1 + Stage 2 (Data scopes)" : "Stage 1 only (Fast Branding)";
    }
    return on;
  }

  oth.addEventListener("change", function () {
    ot.style.display = oth.checked ? "block" : "none";
    syncStage2();
  });
  f.querySelectorAll("[name=google_data]").forEach(function (box) {
    if (box === oth) return;
    box.addEventListener("change", syncStage2);
  });
  syncStage2();

  window.validateIntake = function (form) {
    var ok = true;
    var first = null;
    function bad(el) {
      ok = false;
      first = first || el;
      mark(el, false);
    }
    var on = syncStage2();
    form.querySelectorAll(".fld").forEach(function (d) {
      if (d.hasAttribute("data-req")) {
        var input = d.querySelector("input");
        var value = input.value.trim();
        var good = value.length > 0;
        if (input.type === "email") good = validEmail(value);
        if (input.type === "url") good = validHttps(value);
        if (input.name === "project_id") good = /^[a-z][a-z0-9-]{4,28}[a-z0-9]$/.test(value);
        if (!mark(d, good)) bad(d);
        return;
      }
      if (d.id === "logo-wrap") {
        var logo = d.querySelector("input").value.trim();
        var logoGood = true;
        var logoErr = d.querySelector(".err");
        if (logo) {
          if (looksLikeSecret(logo)) {
            logoGood = false;
            if (logoErr) logoErr.textContent = SECRET_MSG;
          } else if (!validHttps(logo)) {
            logoGood = false;
            if (logoErr) logoErr.textContent = "Please paste a public https:// link, or leave this blank.";
          }
        }
        if (!mark(d, logoGood)) bad(d);
        return;
      }
      if (d.id === "domain") {
        if (!mark(d, !!form.querySelector("[name=domain_verified]:checked"))) bad(d);
        return;
      }
      if (d.id === "data") {
        var dataOk = !!form.querySelector("[name=google_data]:checked");
        if (oth.checked && !ot.value.trim()) dataOk = false;
        if (!mark(d, dataOk)) bad(d);
        return;
      }
      if (d.id === "picker") {
        if (!on) { mark(d, true); return; }
        if (!mark(d, !!form.querySelector("[name=picker_fast_track]:checked"))) bad(d);
        return;
      }
      if (d.id === "scope-wrap") {
        if (!on) { mark(d, true); return; }
        var scope = document.getElementById("scope").value;
        var secret = looksLikeSecret(scope);
        var scopeGood = scope.trim().length > 0 && !secret;
        var scopeErr = d.querySelector(".err");
        if (scopeErr) scopeErr.textContent = secret ? SECRET_MSG : "Please write 2–3 plain sentences about how the app uses that Google data.";
        if (!mark(d, scopeGood)) bad(d);
        return;
      }
      if (d.id === "demo-wrap") {
        if (!on) { mark(d, true); return; }
        var demo = d.querySelector("input").value.trim();
        if (!mark(d, !demo || validHttps(demo))) bad(d);
        return;
      }
      if (d.id === "plan") {
        if (!mark(d, !!form.querySelector("[name=plan]:checked"))) bad(d);
        return;
      }
      if (d.id === "access") {
        if (!mark(d, !!form.querySelector("[name=added_access]:checked"))) bad(d);
        return;
      }
      if (d.id === "notes-wrap") {
        if (!mark(d, !looksLikeSecret(document.getElementById("notes").value))) bad(d);
        return;
      }
      if (d.id === "agree") {
        if (!mark(d, !!form.querySelector("[name=agree_terms]:checked"))) bad(d);
      }
    });
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
      else window.alert("Sorry, the form did not send. Please email verify@velda.ai.");
    }).catch(function () {
      window.alert("Sorry, the form did not send. Check your connection and try again, or email verify@velda.ai.");
    });
  });
})();
