/* Phone UI: big capture button + location scan + offline-tolerant queue.
 *
 * Capture uses <input type="file" capture> — the native camera, which
 * works on iOS Safari over plain HTTP on the LAN (getUserMedia does not).
 * Location scanning likewise takes a still photo and decodes the QR
 * locally with jsQR, so it needs no secure origin either.
 *
 * If an upload fails (server briefly unreachable), the photo is queued in
 * IndexedDB and retried when the connection returns.
 */
(function () {
  "use strict";

  var status = document.getElementById("m-status");
  var captureBtn = document.getElementById("m-capture-btn");
  var captureFile = document.getElementById("m-capture-file");
  var scanBtn = document.getElementById("m-scan-btn");
  var scanFile = document.getElementById("m-scan-file");
  var canvas = document.getElementById("m-canvas");

  function setStatus(text) {
    if (status) status.textContent = text || "";
  }

  // ------------------------------------------------ IndexedDB queue

  function openDb() {
    return new Promise(function (resolve, reject) {
      var request = indexedDB.open("ravens-nest", 1);
      request.onupgradeneeded = function () {
        request.result.createObjectStore("captures", { autoIncrement: true });
      };
      request.onsuccess = function () { resolve(request.result); };
      request.onerror = function () { reject(request.error); };
    });
  }

  function queueCapture(blob) {
    return openDb().then(function (db) {
      return new Promise(function (resolve, reject) {
        var tx = db.transaction("captures", "readwrite");
        tx.objectStore("captures").add({ blob: blob, ts: Date.now() });
        tx.oncomplete = resolve;
        tx.onerror = function () { reject(tx.error); };
      });
    });
  }

  function queuedCount() {
    return openDb().then(function (db) {
      return new Promise(function (resolve) {
        var request = db.transaction("captures").objectStore("captures").count();
        request.onsuccess = function () { resolve(request.result); };
        request.onerror = function () { resolve(0); };
      });
    });
  }

  function upload(blob) {
    var body = new FormData();
    body.append("photo", blob, "capture.jpg");
    return fetch("/capture", { method: "POST", body: body }).then(function (resp) {
      if (!resp.ok) throw new Error("HTTP " + resp.status);
      return resp.json();
    });
  }

  function flushQueue() {
    openDb().then(function (db) {
      var store = db.transaction("captures", "readwrite").objectStore("captures");
      var request = store.openCursor();
      request.onsuccess = function () {
        var cursor = request.result;
        if (!cursor) { refreshStatus(); return; }
        upload(cursor.value.blob)
          .then(function () {
            cursor.delete();
            cursor.continue();
          })
          .catch(function () {
            refreshStatus(); // still unreachable — try again later
          });
      };
    });
  }

  function refreshStatus() {
    queuedCount().then(function (count) {
      setStatus(count ? count + " capture(s) queued — will retry when the server is reachable" : "");
    });
  }

  window.addEventListener("online", flushQueue);
  setInterval(flushQueue, 30000);
  refreshStatus();

  // ------------------------------------------------ capture button

  if (captureBtn && captureFile) {
    captureBtn.addEventListener("click", function () { captureFile.click(); });
    captureFile.addEventListener("change", function () {
      var file = captureFile.files[0];
      captureFile.value = "";
      if (!file) return;
      setStatus("Uploading…");
      upload(file)
        .then(function (data) {
          setStatus(
            data.status === "new"
              ? "Captured — identification queued for review."
              : "Already captured (" + data.status + ")."
          );
        })
        .catch(function () {
          queueCapture(file).then(function () {
            refreshStatus();
          });
        });
    });
  }

  // ------------------------------------------------ location scan

  function decodeQr(file) {
    return new Promise(function (resolve, reject) {
      var url = URL.createObjectURL(file);
      var img = new Image();
      img.onload = function () {
        var scale = Math.min(1, 1200 / img.width); // jsQR is happier below ~1200px
        canvas.width = Math.round(img.width * scale);
        canvas.height = Math.round(img.height * scale);
        var ctx = canvas.getContext("2d", { willReadFrequently: true });
        ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
        URL.revokeObjectURL(url);
        var data = ctx.getImageData(0, 0, canvas.width, canvas.height);
        var hit = window.jsQR(data.data, data.width, data.height);
        if (hit && hit.data) resolve(hit.data);
        else reject(new Error("no QR code found in photo"));
      };
      img.onerror = function () { reject(new Error("could not read photo")); };
      img.src = url;
    });
  }

  if (scanBtn && scanFile) {
    scanBtn.addEventListener("click", function () { scanFile.click(); });
    scanFile.addEventListener("change", function () {
      var file = scanFile.files[0];
      scanFile.value = "";
      if (!file) return;
      setStatus("Reading label…");
      decodeQr(file)
        .then(function (code) {
          setStatus("Scanned " + code);
          var search = document.querySelector(".m-search");
          if (search) search.value = code;
          window.htmx.ajax("GET", "/command?q=" + encodeURIComponent(code), "#m-result");
        })
        .catch(function (err) {
          setStatus(err.message + " — try again, closer and well-lit.");
        });
    });
  }
})();
