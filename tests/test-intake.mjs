// Loads the real intake page in headless Chrome and checks Stage 1 / Stage 2 rules.
import { spawn } from "node:child_process";
import fs from "node:fs";
import http from "node:http";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "verify.velda.ai");
const PAGE_TEST = String.raw`
() => {
  const form = document.getElementById("f");
  const failures = [];
  let checks = 0;
  function assert(name, cond) {
    checks += 1;
    if (!cond) failures.push(name);
  }
  const byId = (id) => document.getElementById(id);
  const SECRET = "Take out passwords, card numbers, API keys, tokens, and client secrets. Email those to nobody.";
  const STAGE1 = "Stage 1 only (Fast Branding)";
  const STAGE2 = "Stage 1 + Stage 2 (Data scopes)";
  function radios(name) {
    return Array.from(form.querySelectorAll('[name="' + name + '"]'));
  }
  function pick(name, value) {
    radios(name).forEach((el) => { el.checked = el.value === value; });
  }
  function setData(values) {
    radios("google_data").forEach((el) => {
      el.checked = values.indexOf(el.value) !== -1;
      el.dispatchEvent(new Event("change", { bubbles: true }));
    });
  }
  function entries() {
    const out = {};
    for (const pair of new FormData(form).entries()) {
      if (!out[pair[0]]) out[pair[0]] = [];
      out[pair[0]].push(pair[1]);
    }
    return out;
  }
  function fillBase() {
    byId("name").value = "Ada Lovelace";
    byId("email").value = "ada@example.com";
    byId("app").value = "Ada Notes";
    byId("home").value = "https://ada.example.com";
    byId("priv").value = "https://ada.example.com/privacy";
    byId("terms").value = "https://ada.example.com/terms";
    byId("support").value = "support@ada.example.com";
    byId("logo").value = "";
    pick("domain_verified", "Yes, verified in Search Console");
    byId("pid").value = "ada-notes-123456";
    byId("othtxt").value = "";
    setData(["Sign-in only"]);
    pick("plan", "DIY guides $100");
    pick("added_access", "Not yet");
    byId("notes").value = "Ready when you are.";
    radios("agree_terms").forEach((el) => { el.checked = true; });
    radios("picker_fast_track").forEach((el) => { el.checked = false; });
    byId("scope").value = "";
    byId("demo").value = "";
  }
  function bad(id) {
    return byId(id).classList.contains("bad");
  }

  assert("validator is live", typeof window.validateIntake === "function");
  assert("form still posts to Formspree", form.getAttribute("action") === "https://formspree.io/f/xdeejbnl");
  assert("stage 2 starts hidden", byId("stage2-wrap").hidden === true);
  assert("stage 2 starts undisplayed", getComputedStyle(byId("stage2-wrap")).display === "none");
  assert("track starts on branding", byId("verification_track").value === STAGE1);

  fillBase();
  let res = window.validateIntake(form);
  assert("sign-in only passes", res.ok === true);
  assert("sign-in keeps stage 2 hidden", byId("stage2-wrap").hidden === true);
  assert("picker stays disabled", radios("picker_fast_track").every((el) => el.disabled));
  assert("scope stays disabled", byId("scope").disabled === true);
  assert("demo stays disabled", byId("demo").disabled === true);
  let body = entries();
  assert("payload track is stage 1", body.verification_track && body.verification_track[0] === STAGE1);
  assert("payload keeps terms", body.terms_url && body.terms_url[0] === "https://ada.example.com/terms");
  assert("payload keeps support email", body.support_email && body.support_email[0] === "support@ada.example.com");
  assert("blank logo is sent as blank", body.logo_url && body.logo_url[0] === "");
  assert("domain answer is sent", body.domain_verified && body.domain_verified[0] === "Yes, verified in Search Console");
  assert("hidden picker is omitted", !body.picker_fast_track);
  assert("hidden scope is omitted", !body.scope_usage);
  assert("hidden demo is omitted", !body.demo_video_url);
  assert("sign-in value is sent", body.google_data && body.google_data.indexOf("Sign-in only") !== -1);

  fillBase();
  byId("terms").value = "";
  res = window.validateIntake(form);
  assert("empty terms fails", res.ok === false && res.first.querySelector("input").id === "terms");
  fillBase();
  byId("terms").value = "http://ada.example.com/terms";
  res = window.validateIntake(form);
  assert("http terms fails", res.ok === false && res.first.querySelector("input").id === "terms");
  fillBase();
  byId("home").value = "http://ada.example.com";
  assert("http homepage still fails", window.validateIntake(form).ok === false);
  fillBase();
  byId("priv").value = "not a url";
  assert("bad privacy link still fails", window.validateIntake(form).ok === false);
  fillBase();
  byId("support").value = "not-an-email";
  assert("bad support email fails", window.validateIntake(form).ok === false && bad("support") === false);
  assert("bad support email marks the field", document.querySelector('[data-req="support"]').classList.contains("bad"));
  fillBase();
  byId("support").value = "";
  assert("empty support email fails", window.validateIntake(form).ok === false);
  fillBase();
  byId("email").value = "ada@";
  assert("bad contact email still fails", window.validateIntake(form).ok === false);
  fillBase();
  byId("logo").value = "   ";
  assert("whitespace logo is optional", window.validateIntake(form).ok === true);
  fillBase();
  byId("logo").value = "http://cdn.example.com/logo.png";
  assert("http logo fails", window.validateIntake(form).ok === false && bad("logo-wrap"));
  fillBase();
  byId("logo").value = "https://cdn.example.com/AIzaSyA1234567890abcdefghij.png";
  res = window.validateIntake(form);
  assert("logo with an API key fails", res.ok === false && bad("logo-wrap"));
  assert("logo secret uses the secret message", byId("logo-wrap").querySelector(".err").textContent === SECRET);
  fillBase();
  byId("logo").value = "https://cdn.example.com/logo.png";
  res = window.validateIntake(form);
  assert("https logo passes", res.ok === true);
  assert("https logo is in the payload", entries().logo_url[0] === "https://cdn.example.com/logo.png");
  fillBase();
  radios("domain_verified").forEach((el) => { el.checked = false; });
  assert("domain answer is required", window.validateIntake(form).ok === false && bad("domain"));
  fillBase();
  pick("domain_verified", "Not yet (tell me what TXT record to add)");
  res = window.validateIntake(form);
  assert("not-yet domain answer passes", res.ok === true);
  assert("not-yet domain is sent", entries().domain_verified[0] === "Not yet (tell me what TXT record to add)");
  fillBase();
  byId("pid").value = "AB";
  assert("bad project id still fails", window.validateIntake(form).ok === false);
  fillBase();
  byId("name").value = " ";
  assert("blank name still fails", window.validateIntake(form).ok === false);
  fillBase();
  setData([]);
  assert("no google data still fails", window.validateIntake(form).ok === false && bad("data"));
  fillBase();
  setData(["Other"]);
  byId("othtxt").value = "";
  assert("other without a description fails", window.validateIntake(form).ok === false && bad("data"));
  fillBase();
  pick("plan", "");
  radios("plan").forEach((el) => { el.checked = false; });
  assert("plan is still required", window.validateIntake(form).ok === false && bad("plan"));
  fillBase();
  radios("added_access").forEach((el) => { el.checked = false; });
  assert("access answer is still required", window.validateIntake(form).ok === false && bad("access"));
  fillBase();
  radios("agree_terms").forEach((el) => { el.checked = false; });
  assert("agreement is still required", window.validateIntake(form).ok === false && bad("agree"));
  fillBase();
  byId("notes").value = "-----BEGIN PRIVATE KEY-----\nabc";
  assert("notes still block a private key", window.validateIntake(form).ok === false && bad("notes-wrap"));
  fillBase();
  byId("notes").value = "card 4242424242424242";
  assert("notes still block a card number", window.validateIntake(form).ok === false);
  fillBase();
  byId("notes").value = "token sk_live_abc123";
  assert("notes still block a live secret key", window.validateIntake(form).ok === false);
  fillBase();
  byId("notes").value = "token sk_test_abc123";
  assert("notes still block a test secret key", window.validateIntake(form).ok === false);
  fillBase();
  byId("notes").value = "ya29.a0AfH6SMBexampletoken";
  assert("notes still block a google token", window.validateIntake(form).ok === false);

  const sensitive = ["Google Drive", "Gmail", "Calendar", "Sheets/Docs", "Other"];
  sensitive.forEach((value) => {
    fillBase();
    if (value === "Other") byId("othtxt").value = "Contacts";
    setData([value]);
    assert(value + " opens stage 2", byId("stage2-wrap").hidden === false);
    assert(value + " is displayed", getComputedStyle(byId("stage2-wrap")).display !== "none");
    assert(value + " enables scope", byId("scope").disabled === false);
    assert(value + " sets the track", byId("verification_track").value === STAGE2);
    res = window.validateIntake(form);
    assert(value + " requires the fast-track answer", res.ok === false && bad("picker"));
    assert(value + " requires the usage answer", bad("scope-wrap"));
    assert(value + " does not require a video yet", bad("demo-wrap") === false);
  });

  fillBase();
  setData(["Sign-in only", "Gmail"]);
  assert("sign-in plus gmail opens stage 2", byId("stage2-wrap").hidden === false);
  pick("picker_fast_track", "Yes — picking individual files works for us");
  byId("scope").value = "   ";
  assert("whitespace usage fails", window.validateIntake(form).ok === false && bad("scope-wrap"));
  const secrets = [
    "-----BEGIN PRIVATE KEY-----\nabc",
    "sk_live_abc123",
    "sk_test_abc123",
    "AIzaSyA1234567890abcdefghij",
    "ya29.a0AfH6SMBexampletoken",
    "4242424242424242"
  ];
  secrets.forEach((secret) => {
    fillBase();
    setData(["Google Drive"]);
    pick("picker_fast_track", "Not sure — advise me on the fastest option");
    byId("scope").value = "We read one file. " + secret;
    res = window.validateIntake(form);
    assert("usage blocks " + secret.slice(0, 18), res.ok === false && bad("scope-wrap"));
    assert("usage secret message for " + secret.slice(0, 12), byId("scope-wrap").querySelector(".err").textContent === SECRET);
  });

  fillBase();
  setData(["Calendar"]);
  pick("picker_fast_track", "No — our app needs broader background access");
  byId("scope").value = "When a person connects a calendar, we read the next meeting so we can show it on their home screen. We do not change their events.";
  byId("demo").value = "";
  res = window.validateIntake(form);
  assert("stage 2 passes without a video", res.ok === true);
  body = entries();
  assert("stage 2 track is sent", body.verification_track[0] === STAGE2);
  assert("fast-track answer is sent", body.picker_fast_track[0] === "No — our app needs broader background access");
  assert("usage answer is sent", body.scope_usage[0].indexOf("next meeting") !== -1);
  assert("blank video is sent while stage 2 is open", body.demo_video_url && body.demo_video_url[0] === "");
  assert("calendar is sent", body.google_data.indexOf("Calendar") !== -1);
  byId("demo").value = "http://youtu.be/demo";
  assert("http video fails", window.validateIntake(form).ok === false && bad("demo-wrap"));
  byId("demo").value = "https://www.youtube.com/watch?v=abcdefghijk";
  res = window.validateIntake(form);
  assert("https video passes", res.ok === true);
  assert("https video is sent", entries().demo_video_url[0] === "https://www.youtube.com/watch?v=abcdefghijk");

  fillBase();
  setData(["Sheets/Docs", "Google Drive"]);
  pick("picker_fast_track", "Yes — picking individual files works for us");
  byId("scope").value = "sk_test_leftoversecret";
  byId("demo").value = "http://evil.example/demo";
  setData(["Sign-in only"]);
  assert("collapsing hides stage 2", byId("stage2-wrap").hidden === true);
  assert("collapsing disables scope", byId("scope").disabled === true);
  assert("collapsing disables the video", byId("demo").disabled === true);
  assert("collapsing disables the fast-track radios", radios("picker_fast_track").every((el) => el.disabled));
  assert("other text hides again", byId("othtxt").style.display === "none");
  res = window.validateIntake(form);
  assert("collapsed form does not block on stage 2", res.ok === true);
  body = entries();
  assert("collapsed track is stage 1", body.verification_track[0] === STAGE1);
  assert("collapsed payload drops the fast-track answer", !body.picker_fast_track);
  assert("collapsed payload drops the usage answer", !body.scope_usage);
  assert("collapsed payload drops the video", !body.demo_video_url);
  assert("collapsed payload drops the leftover secret", JSON.stringify(body).indexOf("sk_test_") === -1);

  fillBase();
  setData(["Other"]);
  assert("other shows the extra text box", byId("othtxt").style.display === "block");
  byId("othtxt").value = "Google Contacts";
  pick("picker_fast_track", "Not sure — advise me on the fastest option");
  byId("scope").value = "We read the contact a person picks so we can address an email. We do not store the whole address book.";
  assert("other plus stage 2 answers passes", window.validateIntake(form).ok === true);
  body = entries();
  assert("other description is sent", body.google_data_other[0] === "Google Contacts");
  assert("other track is stage 2", body.verification_track[0] === STAGE2);

  fillBase();
  byId("scope").value = "sk_live_shouldnotsend";
  byId("demo").value = "http://not-sent.example/demo";
  pick("picker_fast_track", "Yes — picking individual files works for us");
  res = window.validateIntake(form);
  assert("secrets typed into a hidden stage 2 do not block", res.ok === true);
  body = entries();
  assert("hidden secret is not in the payload", JSON.stringify(body).indexOf("sk_live_shouldnotsend") === -1);

  let posted = null;
  window.fetch = (url, opts) => {
    const sent = [];
    for (const pair of opts.body.entries()) sent.push([pair[0], pair[1]]);
    posted = { url: String(url), sent: sent };
    return Promise.resolve({ ok: true });
  };
  fillBase();
  byId("email").value = "nope";
  form.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
  assert("invalid submit does not post", posted === null);

  fillBase();
  setData(["Gmail"]);
  byId("scope").value = "sk_test_notposted";
  byId("demo").value = "http://not-posted.example/v";
  pick("picker_fast_track", "Yes — picking individual files works for us");
  setData(["Sign-in only"]);
  form.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
  assert("valid submit posts once", posted !== null);
  assert("submit posts to Formspree", posted && posted.url === "https://formspree.io/f/xdeejbnl");
  if (posted) {
    const sent = {};
    posted.sent.forEach((pair) => {
      if (!sent[pair[0]]) sent[pair[0]] = [];
      sent[pair[0]].push(pair[1]);
    });
    assert("submit track is stage 1", sent.verification_track && sent.verification_track[0] === STAGE1);
    assert("submit omits fast-track", !sent.picker_fast_track);
    assert("submit omits usage", !sent.scope_usage);
    assert("submit omits video", !sent.demo_video_url);
    assert("submit omits the hidden secret", JSON.stringify(sent).indexOf("sk_test_notposted") === -1);
    assert("submit still sends the app name", sent.app_name && sent.app_name[0] === "Ada Notes");
    assert("submit still sends agreement", sent.agree_terms && sent.agree_terms[0] === "yes");
  }

  return { failures: failures, checks: checks };
}
`;

function startServer() {
  return new Promise((resolve, reject) => {
    const server = http.createServer((req, res) => {
      const url = new URL(req.url || "/", "http://127.0.0.1");
      let rel = decodeURIComponent(url.pathname);
      if (rel.endsWith("/")) rel += "index.html";
      const file = path.normalize(path.join(root, rel));
      if (!file.startsWith(root)) {
        res.writeHead(403);
        res.end("no");
        return;
      }
      fs.readFile(file, (err, data) => {
        if (err) {
          res.writeHead(404);
          res.end("missing");
          return;
        }
        const ext = path.extname(file);
        const type = ext === ".js" ? "text/javascript" : ext === ".css" ? "text/css" : "text/html; charset=utf-8";
        res.writeHead(200, { "Content-Type": type });
        res.end(data);
      });
    });
    server.listen(0, "127.0.0.1", () => resolve(server));
    server.on("error", reject);
  });
}

function asText(data) {
  if (typeof data === "string") return Promise.resolve(data);
  if (Buffer.isBuffer(data)) return Promise.resolve(data.toString("utf8"));
  if (data instanceof ArrayBuffer) return Promise.resolve(Buffer.from(data).toString("utf8"));
  if (data && typeof data.text === "function") return data.text();
  return Promise.resolve(String(data));
}

class Cdp {
  constructor(ws) {
    this.ws = ws;
    this.next = 1;
    this.pending = new Map();
    ws.addEventListener("message", (ev) => {
      asText(ev.data).then((raw) => {
        const msg = JSON.parse(raw);
        if (msg.id && this.pending.has(msg.id)) {
          const waiting = this.pending.get(msg.id);
          this.pending.delete(msg.id);
          if (msg.error) waiting.reject(new Error(JSON.stringify(msg.error)));
          else waiting.resolve(msg.result);
        }
      }).catch((err) => {
        console.error(err);
      });
    });
  }

  send(method, params = {}, sessionId) {
    const id = this.next++;
    const payload = { id, method, params };
    if (sessionId) payload.sessionId = sessionId;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.ws.send(JSON.stringify(payload));
    });
  }
}

async function launchChrome() {
  const userDir = fs.mkdtempSync(path.join("/tmp", "intake-chrome-"));
  const proc = spawn("google-chrome", [
    "--headless=new",
    "--disable-gpu",
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-background-networking",
    "--remote-debugging-port=0",
    "--remote-allow-origins=*",
    `--user-data-dir=${userDir}`,
    "about:blank",
  ], { stdio: ["ignore", "pipe", "pipe"] });
  let buf = "";
  const browserWs = await new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error("chrome did not open a devtools port\n" + buf)), 20000);
    const onData = (chunk) => {
      buf += chunk.toString();
      const match = buf.match(/ws:\/\/127\.0\.0\.1:\d+\/devtools\/browser\/[A-Za-z0-9-]+/);
      if (match) {
        clearTimeout(timer);
        resolve(match[0]);
      }
    };
    proc.stdout.on("data", onData);
    proc.stderr.on("data", onData);
    proc.on("exit", (code) => {
      clearTimeout(timer);
      reject(new Error("chrome exited " + code + "\n" + buf));
    });
  });
  return { proc, userDir, browserWs };
}

async function main() {
  const server = await startServer();
  const port = server.address().port;
  const pageUrl = `http://127.0.0.1:${port}/intake.html`;
  let chrome = null;
  try {
    chrome = await launchChrome();
    const ws = new WebSocket(chrome.browserWs);
    await new Promise((resolve, reject) => {
      ws.addEventListener("open", resolve);
      ws.addEventListener("error", () => reject(new Error("devtools websocket failed")));
    });
    const cdp = new Cdp(ws);
    const created = await cdp.send("Target.createTarget", { url: "about:blank" });
    const attached = await cdp.send("Target.attachToTarget", { targetId: created.targetId, flatten: true });
    const sessionId = attached.sessionId;
    await cdp.send("Runtime.enable", {}, sessionId);
    await cdp.send("Page.enable", {}, sessionId);
    await cdp.send("Page.navigate", { url: pageUrl }, sessionId);
    let ready = false;
    for (let i = 0; i < 50; i++) {
      const probe = await cdp.send("Runtime.evaluate", {
        expression: "document.readyState === 'complete' && typeof window.validateIntake === 'function'",
        returnByValue: true,
      }, sessionId);
      if (probe.result && probe.result.value === true) {
        ready = true;
        break;
      }
      await new Promise((resolve) => setTimeout(resolve, 100));
    }
    if (!ready) throw new Error("intake page did not load validateIntake");
    const evaluated = await cdp.send("Runtime.evaluate", {
      expression: `(${PAGE_TEST})()`,
      returnByValue: true,
      awaitPromise: true,
    }, sessionId);
    if (evaluated.exceptionDetails) {
      console.error(JSON.stringify(evaluated.exceptionDetails, null, 2));
      process.exitCode = 1;
      return;
    }
    const report = evaluated.result.value;
    if (!report || !Array.isArray(report.failures)) {
      console.error("unexpected report", report);
      process.exitCode = 1;
      return;
    }
    for (const failure of report.failures) console.error("FAIL:", failure);
    if (report.failures.length) {
      process.exitCode = 1;
      return;
    }
    console.log(`intake validation ok: ${report.checks} checks`);
    ws.close();
  } finally {
    if (chrome) chrome.proc.kill("SIGKILL");
    server.close();
    if (chrome) fs.rmSync(chrome.userDir, { recursive: true, force: true });
  }
}

main().catch((err) => {
  console.error(err);
  process.exitCode = 1;
});
