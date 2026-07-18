/* Keyboard-first command bar: ↑↓ move the selection through result items,
 * Enter acts on the selection (or submits the command when nothing is
 * selected), Esc clears everything. */
(function () {
  "use strict";

  var input = document.getElementById("cmd");
  var form = document.getElementById("cmd-form");
  var result = document.getElementById("result");
  if (!input || !form || !result) return;

  var selected = -1;

  function items() {
    return Array.prototype.slice.call(result.querySelectorAll(".result-item"));
  }

  function paint() {
    items().forEach(function (el, i) {
      el.classList.toggle("sel", i === selected);
      if (i === selected) el.scrollIntoView({ block: "nearest" });
    });
  }

  document.body.addEventListener("htmx:afterSwap", function (event) {
    if (event.target === result) {
      selected = -1;
      paint();
    }
  });

  input.addEventListener("keydown", function (event) {
    var list = items();
    if (event.key === "ArrowDown" && list.length) {
      event.preventDefault();
      selected = Math.min(selected + 1, list.length - 1);
      paint();
    } else if (event.key === "ArrowUp" && list.length) {
      event.preventDefault();
      selected = Math.max(selected - 1, -1);
      paint();
    } else if (event.key === "Enter" && selected >= 0 && list[selected]) {
      event.preventDefault();
      list[selected].click();
    } else if (event.key === "Escape") {
      event.preventDefault();
      input.value = "";
      result.innerHTML = "";
      selected = -1;
      input.focus();
    } else if (event.key.length === 1 || event.key === "Backspace") {
      selected = -1; // typing resets the selection
    }
  });

  // Keep focus on the bar after actions resolve elsewhere on the page.
  document.body.addEventListener("htmx:afterSwap", function (event) {
    if (event.target === result) input.focus();
  });

  // Rotate example commands through the placeholder while the bar is empty,
  // so features are discoverable without reading anything.
  try {
    var examples = JSON.parse(input.getAttribute("data-examples") || "[]");
    if (examples.length > 1) {
      var exampleIndex = 0;
      setInterval(function () {
        if (input.value === "") {
          exampleIndex = (exampleIndex + 1) % examples.length;
          input.placeholder = examples[exampleIndex];
        }
      }, 4000);
    }
  } catch (e) {
    /* placeholder rotation is decoration — never break the bar over it */
  }
})();
