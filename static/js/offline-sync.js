/* Offline queue + auto-sync for POST forms with files. */
(function () {
    "use strict";

    var DB_NAME = "cenro_offline_queue";
    var DB_VERSION = 1;
    var STORE = "requests";
    var RETRY_INTERVAL_MS = 15000;
    var syncing = false;

    function openDb() {
        return new Promise(function (resolve, reject) {
            var req = indexedDB.open(DB_NAME, DB_VERSION);
            req.onupgradeneeded = function () {
                var db = req.result;
                if (!db.objectStoreNames.contains(STORE)) {
                    var store = db.createObjectStore(STORE, { keyPath: "id" });
                    store.createIndex("createdAt", "createdAt", { unique: false });
                }
            };
            req.onsuccess = function () { resolve(req.result); };
            req.onerror = function () { reject(req.error); };
        });
    }

    function dbTx(mode, fn) {
        return openDb().then(function (db) {
            return new Promise(function (resolve, reject) {
                var tx = db.transaction(STORE, mode);
                var store = tx.objectStore(STORE);
                fn(store, resolve, reject);
                tx.onerror = function () { reject(tx.error); };
                tx.oncomplete = function () { db.close(); };
            });
        });
    }

    function putItem(item) {
        return dbTx("readwrite", function (store, resolve) {
            store.put(item);
            resolve();
        });
    }

    function deleteItem(id) {
        return dbTx("readwrite", function (store, resolve) {
            store.delete(id);
            resolve();
        });
    }

    function getAllItems() {
        return dbTx("readonly", function (store, resolve, reject) {
            var req = store.getAll();
            req.onsuccess = function () { resolve(req.result || []); };
            req.onerror = function () { reject(req.error); };
        });
    }

    function randomId() {
        return "q_" + Date.now() + "_" + Math.random().toString(16).slice(2);
    }

    function readFileAsBytes(file) {
        return new Promise(function (resolve, reject) {
            var reader = new FileReader();
            reader.onload = function () {
                var bytes = Array.from(new Uint8Array(reader.result));
                resolve(bytes);
            };
            reader.onerror = function () { reject(reader.error); };
            reader.readAsArrayBuffer(file);
        });
    }

    async function serializeForm(form) {
        var data = new FormData(form);
        var entries = [];
        for (const pair of data.entries()) {
            var key = pair[0];
            var value = pair[1];
            if (value instanceof File) {
                if (!value.name && value.size === 0) {
                    // Keep empty file slot as empty string for consistent server behavior.
                    entries.push({ type: "text", key: key, value: "" });
                    continue;
                }
                var bytes = await readFileAsBytes(value);
                entries.push({
                    type: "file",
                    key: key,
                    name: value.name || "upload.bin",
                    mime: value.type || "application/octet-stream",
                    bytes: bytes,
                    lastModified: value.lastModified || Date.now()
                });
            } else {
                entries.push({ type: "text", key: key, value: String(value) });
            }
        }
        return entries;
    }

    function deserializeToFormData(entries) {
        var fd = new FormData();
        for (var i = 0; i < entries.length; i++) {
            var e = entries[i];
            if (e.type === "file") {
                var blob = new Blob([new Uint8Array(e.bytes)], { type: e.mime || "application/octet-stream" });
                var file = new File([blob], e.name || "upload.bin", {
                    type: e.mime || "application/octet-stream",
                    lastModified: e.lastModified || Date.now()
                });
                fd.append(e.key, file);
            } else {
                fd.append(e.key, e.value || "");
            }
        }
        return fd;
    }

    function ensureBanner() {
        var existing = document.getElementById("offlineSyncBanner");
        if (existing) return existing;
        var el = document.createElement("div");
        el.id = "offlineSyncBanner";
        el.style.cssText = "position:fixed;right:16px;bottom:16px;z-index:9999;background:#1e7a4d;color:#fff;padding:10px 12px;border-radius:8px;font-size:12px;box-shadow:0 4px 12px rgba(0,0,0,.2);display:none;max-width:280px;";
        document.body.appendChild(el);
        return el;
    }

    function showBanner(msg, bg) {
        var el = ensureBanner();
        el.textContent = msg;
        if (bg) el.style.background = bg;
        el.style.display = "block";
        clearTimeout(el._hideTimer);
        el._hideTimer = setTimeout(function () {
            el.style.display = "none";
        }, 3200);
    }

    function updateOnlineState() {
        if (navigator.onLine) return;
        showBanner("Offline mode: submissions will be queued.", "#c27b00");
    }

    async function enqueueForm(form) {
        var entries = await serializeForm(form);
        var item = {
            id: randomId(),
            url: form.getAttribute("action") || window.location.href,
            method: (form.getAttribute("method") || "POST").toUpperCase(),
            entries: entries,
            label: form.getAttribute("data-offline-label") || "Queued form submission",
            createdAt: Date.now(),
            retries: 0
        };
        await putItem(item);
        var pending = (await getAllItems()).length;
        showBanner(item.label + " saved offline. Pending sync: " + pending, "#1e7a4d");
    }

    async function enqueueFormData(url, method, label, formData) {
        var entries = [];
        for (const pair of formData.entries()) {
            var key = pair[0];
            var value = pair[1];
            if (value instanceof File) {
                if (!value.name && value.size === 0) {
                    entries.push({ type: "text", key: key, value: "" });
                    continue;
                }
                var bytes = await readFileAsBytes(value);
                entries.push({
                    type: "file",
                    key: key,
                    name: value.name || "upload.bin",
                    mime: value.type || "application/octet-stream",
                    bytes: bytes,
                    lastModified: value.lastModified || Date.now()
                });
            } else {
                entries.push({ type: "text", key: key, value: String(value) });
            }
        }
        var item = {
            id: randomId(),
            url: url || window.location.href,
            method: (method || "POST").toUpperCase(),
            entries: entries,
            label: label || "Queued form submission",
            createdAt: Date.now(),
            retries: 0
        };
        await putItem(item);
        var pending = (await getAllItems()).length;
        showBanner(item.label + " saved offline. Pending sync: " + pending, "#1e7a4d");
    }

    async function syncQueue() {
        if (syncing || !navigator.onLine) return;
        syncing = true;
        try {
            var items = await getAllItems();
            if (!items.length) return;
            items.sort(function (a, b) { return a.createdAt - b.createdAt; });
            for (var i = 0; i < items.length; i++) {
                var item = items[i];
                try {
                    var fd = deserializeToFormData(item.entries || []);
                    var resp = await fetch(item.url, {
                        method: item.method || "POST",
                        body: fd,
                        credentials: "include"
                    });
                    // Server-wins: for client errors/conflicts, drop from queue and notify user.
                    if (resp.status >= 400 && resp.status < 500) {
                        await deleteItem(item.id);
                        showBanner("Sync skipped (server rejected): " + (item.label || "request"), "#8a1f1f");
                        continue;
                    }
                    if (!resp.ok) {
                        item.retries = (item.retries || 0) + 1;
                        await putItem(item);
                        break;
                    }
                    await deleteItem(item.id);
                } catch (err) {
                    item.retries = (item.retries || 0) + 1;
                    await putItem(item);
                    break;
                }
            }
            var remaining = (await getAllItems()).length;
            if (remaining === 0) {
                showBanner("Offline queue synced successfully.", "#1e7a4d");
            } else {
                showBanner("Sync in progress. Pending: " + remaining, "#2d8659");
            }
        } finally {
            syncing = false;
        }
    }

    function bindOfflineForms() {
        var forms = document.querySelectorAll("form[data-offline-sync='true']");
        for (var i = 0; i < forms.length; i++) {
            forms[i].addEventListener("submit", function (evt) {
                if (navigator.onLine) return;
                evt.preventDefault();
                enqueueForm(evt.target).catch(function () {
                    showBanner("Failed to save offline submission.", "#8a1f1f");
                });
            });
        }
    }

    document.addEventListener("DOMContentLoaded", function () {
        bindOfflineForms();
        updateOnlineState();
        syncQueue();
        setInterval(syncQueue, RETRY_INTERVAL_MS);
    });
    window.addEventListener("online", function () {
        showBanner("Back online. Syncing queued submissions...", "#1e7a4d");
        syncQueue();
    });
    window.addEventListener("offline", updateOnlineState);

    window.CenroOffline = {
        enqueueFormData: enqueueFormData,
        syncNow: syncQueue,
        isOnline: function () { return navigator.onLine; }
    };
})();
