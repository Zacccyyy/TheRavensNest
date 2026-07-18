/* Camera QR scanning for the move console.
 *
 * Uses the native BarcodeDetector API where available (Chrome/Android),
 * falling back to the vendored jsQR library via canvas frame grabs —
 * that path is what runs on iOS Safari, which lacks BarcodeDetector.
 * Camera access requires HTTPS or localhost.
 *
 * USB barcode scanners need none of this: they type the code and send
 * Enter, which submits the always-focused #scan-input form.
 */
(function () {
  "use strict";

  var input = document.getElementById("scan-input");
  var form = document.getElementById("scan-form");
  var video = document.getElementById("scan-video");
  var canvas = document.getElementById("scan-canvas");
  var button = document.getElementById("camera-btn");
  if (!input || !form || !button) return;

  // Keep the scan input focused so USB scanner keystrokes always land here.
  document.body.addEventListener("htmx:afterSwap", function () {
    input.focus();
  });

  var scanning = false;
  var stream = null;
  var lastCode = null;
  var lastTime = 0;

  function beep() {
    try {
      var Ctx = window.AudioContext || window.webkitAudioContext;
      var ctx = new Ctx();
      var osc = ctx.createOscillator();
      osc.type = "square";
      osc.frequency.value = 880;
      osc.connect(ctx.destination);
      osc.start();
      osc.stop(ctx.currentTime + 0.08);
    } catch (e) {
      /* no audio available — fine */
    }
  }

  function submitForm(f) {
    // iOS Safari < 16 has no form.requestSubmit; a dispatched submit
    // event goes through htmx's submit listener the same way.
    if (typeof f.requestSubmit === "function") f.requestSubmit();
    else f.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
  }

  function onDecode(code) {
    var now = Date.now();
    if (code === lastCode && now - lastTime < 2500) return; // debounce re-reads
    lastCode = code;
    lastTime = now;
    beep();
    input.value = code;
    submitForm(form);
  }

  function nativeLoop(detector) {
    if (!scanning) return;
    detector
      .detect(video)
      .then(function (codes) {
        if (codes.length && codes[0].rawValue) onDecode(codes[0].rawValue);
      })
      .catch(function () {
        /* frame not ready */
      })
      .then(function () {
        setTimeout(function () {
          nativeLoop(detector);
        }, 200);
      });
  }

  function jsqrLoop() {
    if (!scanning) return;
    if (video.readyState === video.HAVE_ENOUGH_DATA && window.jsQR) {
      canvas.width = video.videoWidth;
      canvas.height = video.videoHeight;
      var ctx = canvas.getContext("2d", { willReadFrequently: true });
      ctx.drawImage(video, 0, 0);
      var img = ctx.getImageData(0, 0, canvas.width, canvas.height);
      var hit = window.jsQR(img.data, img.width, img.height, {
        inversionAttempts: "dontInvert",
      });
      if (hit && hit.data) onDecode(hit.data);
    }
    setTimeout(jsqrLoop, 200);
  }

  function start() {
    return navigator.mediaDevices
      .getUserMedia({ video: { facingMode: "environment" }, audio: false })
      .then(function (mediaStream) {
        stream = mediaStream;
        video.srcObject = stream;
        video.hidden = false;
        return video.play();
      })
      .then(function () {
        scanning = true;
        button.textContent = "⏹ Stop camera";
        if ("BarcodeDetector" in window) {
          return window.BarcodeDetector.getSupportedFormats()
            .then(function (formats) {
              if (formats.indexOf("qr_code") !== -1) {
                nativeLoop(new window.BarcodeDetector({ formats: ["qr_code"] }));
              } else {
                jsqrLoop();
              }
            })
            .catch(jsqrLoop);
        }
        jsqrLoop();
      });
  }

  function stop() {
    scanning = false;
    if (stream) {
      stream.getTracks().forEach(function (track) {
        track.stop();
      });
    }
    stream = null;
    video.hidden = true;
    button.textContent = "📷 Camera";
    input.focus();
  }

  button.addEventListener("click", function () {
    if (scanning) {
      stop();
    } else {
      start().catch(function (err) {
        alert(
          "Camera unavailable: " +
            err.message +
            "\n(Camera access needs HTTPS or localhost.)"
        );
      });
    }
  });
})();
