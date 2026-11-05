/* ════════════════════════════════════════════════════════════════════════
   audit-common.js — shared by form1_on_reaching_store.html and
   form2_on_store_sealing.html. Keep this file next to the two forms.

   What it does, and why:
   • ACCESS KEY. The PIN the auditor types is NOT checked in the page (anything
     in the page source is public). It is sent with every upload and
     submission as the header  X-Audit-Key  and n8n checks it.
     → In n8n, set both webhooks to Header Auth: name X-Audit-Key, value = PIN.
   • PHOTO UPLOADS. One generation counter per field, so re-picking photos
     while an upload is running can never mix old and new photos; per-file
     timeout, automatic retries, and a Retry button for anything that failed.
   • SUBMISSION. Awaited, with timeout and retries; the "Submitted" screen is
     shown only after n8n confirms. On failure the form stays open with a
     Retry button — nothing is silently lost.
   • SAFE OUTPUT. Status rows are built with DOM APIs and textContent (no
     innerHTML), and only https:// links from n8n are shown.
   ════════════════════════════════════════════════════════════════════════ */
"use strict";
(function () {
  const UPLOAD_TIMEOUT_MS = 90000;
  const SUBMIT_TIMEOUT_MS = 30000;
  const MAX_ATTEMPTS = 3;

  let accessKey = "";
  let authRejectedHandler = function () {};
  // "header" (default): X-Audit-Key header -- never lands in the stored data.
  // "body": an auditKey form/JSON field -- use only if your n8n rejects the
  // header in the browser's CORS pre-flight. (Then strip it in n8n before saving.)
  let authInBody = false;

  class AuthRejected extends Error {}

  function setAccessKey(k) { accessKey = String(k || ""); }
  function setAuthInBody(v) { authInBody = !!v; }
  function onAuthRejected(fn) { authRejectedHandler = fn; }

  function authHeaders(extra) {
    const h = Object.assign({}, extra || {});
    if (accessKey && !authInBody) h["X-Audit-Key"] = accessKey;
    return h;
  }

  async function fetchWithTimeout(url, opts, ms) {
    const ac = new AbortController();
    const timer = setTimeout(function () { ac.abort(); }, ms);
    try {
      return await fetch(url, Object.assign({}, opts, { signal: ac.signal }));
    } finally {
      clearTimeout(timer);
    }
  }

  async function withRetry(fn, attempts) {
    let last;
    for (let i = 0; i < attempts; i++) {
      try {
        return await fn();
      } catch (e) {
        last = e;
        if (e instanceof AuthRejected) throw e;           // a wrong PIN won't fix itself
        if (i < attempts - 1) await new Promise(function (r) { setTimeout(r, 1500 * (i + 1)); });
      }
    }
    throw last;
  }

  function checkAuth(res) {
    if (res.status === 401 || res.status === 403) {
      authRejectedHandler();
      throw new AuthRejected("The server did not accept the PIN.");
    }
  }

  /* ── helpers ─────────────────────────────────────────────────────────── */
  const MIME_EXT = { "image/jpeg": "jpg", "image/jpg": "jpg", "image/png": "png", "image/heic": "heic",
                     "image/heif": "heif", "image/webp": "webp", "image/gif": "gif" };
  function fileExt(file) {
    if (MIME_EXT[file.type]) return MIME_EXT[file.type];
    const m = /\.([a-z0-9]{1,5})$/i.exec(file.name || "");
    return m ? m[1].toLowerCase() : "jpg";
  }

  function safeLink(link) {
    try {
      const u = new URL(String(link));
      return u.protocol === "https:" ? u.href : null;
    } catch (_) {
      return null;
    }
  }

  function auditId() {
    if (window.crypto && crypto.randomUUID) return crypto.randomUUID();
    const b = new Uint8Array(16);
    crypto.getRandomValues(b);
    return Array.from(b, function (x) { return x.toString(16).padStart(2, "0"); }).join("");
  }

  // "2026-11-05" → local midnight. new Date("2026-11-05") would be UTC midnight
  // and show the previous day in timezones behind UTC.
  function parseYmd(s) {
    const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(s || "");
    return m ? new Date(+m[1], +m[2] - 1, +m[3]) : null;
  }
  function todayYmd() {
    const d = new Date();
    return d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0") + "-" +
           String(d.getDate()).padStart(2, "0");
  }

  const valid = {
    mobile: function (v) { return /^[6-9]\d{9}$/.test(v); },
    phone: function (v) { return /^\+?\d[\d\s-]{5,15}$/.test(v); },
    email: function (v) { return /^[^\s@]+@[^\s@]+\.[^\s@]{2,}$/.test(v); },
    storeCode: function (v) { return /^[A-Za-z0-9][A-Za-z0-9 _\/-]{1,23}$/.test(v); },
    auditId: function (v) { return /^[0-9a-f-]{16,40}$/i.test(v); },
  };

  /* ── photo field ─────────────────────────────────────────────────────── */
  function createPhotoField(opts) {
    // opts: { inputId, previewId, statusId, label, url, buildName(label,i,ext,date),
    //         buildMeta(), onChange() }
    let gen = 0;
    let items = [];          // { file, name, status: uploading|done|err, link }
    let previewUrls = [];

    function render() {
      const el = document.getElementById(opts.statusId);
      el.replaceChildren();
      items.forEach(function (it, i) {
        const row = document.createElement("div");
        row.className = "upload-item";
        const name = document.createElement("span");
        name.className = "uname";
        name.textContent = it.name;
        const st = document.createElement("span");
        st.className = "ustatus " + it.status;
        st.textContent = it.status === "uploading" ? "Uploading…" : it.status === "done" ? "✅ Saved" : "❌ Failed";
        row.append(name, st);
        if (it.link) {
          const a = document.createElement("a");
          a.href = it.link;
          a.target = "_blank";
          a.rel = "noopener noreferrer";
          a.textContent = "View";
          row.append(a);
        }
        if (it.status === "err") {
          const b = document.createElement("button");
          b.type = "button";
          b.className = "retry-btn";
          b.textContent = "Retry";
          const myGen = gen;
          b.addEventListener("click", function () { uploadOne(i, myGen); });
          row.append(b);
        }
        el.append(row);
      });
    }

    async function uploadOne(i, myGen) {
      const it = items[i];
      if (!it || myGen !== gen) return;
      it.status = "uploading";
      it.link = null;
      render();
      opts.onChange();
      try {
        const link = await withRetry(async function () {
          const form = new FormData();
          form.append("file", it.file, it.name);
          form.append("fileName", it.name);
          const meta = opts.buildMeta();
          Object.keys(meta).forEach(function (k) { form.append(k, meta[k]); });
          if (authInBody && accessKey) form.append("auditKey", accessKey);
          const res = await fetchWithTimeout(opts.url, { method: "POST", body: form, headers: authHeaders() },
                                             UPLOAD_TIMEOUT_MS);
          checkAuth(res);
          if (!res.ok) throw new Error("HTTP " + res.status);
          const data = await res.json();
          const ok = safeLink(data && data.link);
          if (!ok) throw new Error("upload response had no valid https link");
          return ok;
        }, MAX_ATTEMPTS);
        if (myGen !== gen) return;               // photos were re-picked meanwhile: discard
        it.status = "done";
        it.link = link;
      } catch (e) {
        if (myGen !== gen) return;
        it.status = "err";
        console.error("Photo upload failed:", e);
      }
      render();
      opts.onChange();
    }

    function select(fileList) {
      gen++;
      const myGen = gen;
      const files = Array.from(fileList || []);
      const preview = document.getElementById(opts.previewId);
      previewUrls.forEach(function (u) { URL.revokeObjectURL(u); });
      previewUrls = [];
      preview.replaceChildren();
      files.forEach(function (f) {
        const img = document.createElement("img");
        const u = URL.createObjectURL(f);
        previewUrls.push(u);
        img.src = u;
        img.alt = "";
        preview.append(img);
      });
      const now = new Date();
      items = files.map(function (f, i) {
        return { file: f, name: opts.buildName(opts.label, i, fileExt(f), now), status: "uploading", link: null };
      });
      render();
      opts.onChange();
      (async function () {
        for (let i = 0; i < items.length; i++) {
          if (myGen !== gen) return;
          await uploadOne(i, myGen);
        }
      })();
    }

    return {
      select: select,
      done: function () { return items.length > 0 && items.every(function (it) { return it.status === "done"; }); },
      count: function () { return items.length; },
      failed: function () { return items.some(function (it) { return it.status === "err"; }); },
      uploading: function () { return items.some(function (it) { return it.status === "uploading"; }); },
      links: function () {
        return items.filter(function (it) { return it.status === "done"; })
                    .map(function (it) { return { name: it.name, link: it.link }; });
      },
    };
  }

  /* ── copy summary + open WhatsApp, in the same tap ───────────────────── */
  function fallbackCopy(text) {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.setAttribute("readonly", "");
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    let ok = false;
    try { ok = document.execCommand("copy"); } catch (_) { ok = false; }
    document.body.removeChild(ta);
    return ok;
  }

  // Call directly from the click handler, before any await: iOS Safari only
  // allows window.open and clipboard writes during the tap itself.
  function copyAndOpen(text, waLink) {
    let copied;
    if (navigator.clipboard && window.isSecureContext) {
      copied = navigator.clipboard.writeText(text).then(function () { return true; },
                                                        function () { return fallbackCopy(text); });
    } else {
      copied = Promise.resolve(fallbackCopy(text));
    }
    if (waLink && /^https:\/\/chat\.whatsapp\.com\/[A-Za-z0-9]+$/.test(waLink)) {
      window.open(waLink, "_blank", "noopener");
    }
    return copied;
  }

  /* ── final submission ────────────────────────────────────────────────── */
  async function submitJson(url, payload) {
    const body = JSON.stringify(authInBody && accessKey ? Object.assign({}, payload, { auditKey: accessKey })
                                                        : payload);
    return withRetry(async function () {
      const res = await fetchWithTimeout(url, {
        method: "POST",
        headers: authHeaders({ "Content-Type": "application/json" }),
        body: body,
        keepalive: body.length < 60000,         // survives the tab being closed
      }, SUBMIT_TIMEOUT_MS);
      checkAuth(res);
      if (!res.ok) throw new Error("HTTP " + res.status);
      return true;
    }, MAX_ATTEMPTS);
  }

  window.AuditCommon = {
    setAccessKey: setAccessKey, setAuthInBody: setAuthInBody, onAuthRejected: onAuthRejected,
    AuthRejected: AuthRejected,
    createPhotoField: createPhotoField, copyAndOpen: copyAndOpen, submitJson: submitJson,
    auditId: auditId, parseYmd: parseYmd, todayYmd: todayYmd, valid: valid,
  };
})();
