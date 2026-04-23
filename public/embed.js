(function () {
  "use strict";

  var SCRIPT_ID = "nic-agent-embed-script";
  var FRAME_ID = "nic-agent-iframe";
  var CLOSED_SIZE = { width: 84, height: 84 };
  var OPEN_SIZE = { width: 344, height: 440 };
  var EDGE_GAP = 16;
  var loaderScript = document.currentScript || document.getElementById(SCRIPT_ID);

  function boot() {
    if (document.getElementById(FRAME_ID)) {
      return;
    }

    var currentScript = loaderScript || document.getElementById(SCRIPT_ID);
    var scriptSrc = currentScript && currentScript.src ? currentScript.src : window.location.href;
    var widgetUrl =
      (currentScript && currentScript.getAttribute("data-widget-url")) ||
      new URL("/", scriptSrc).toString();

    var iframe = document.createElement("iframe");
    iframe.id = FRAME_ID;
    iframe.title = "NIC Voice Agent";
    iframe.src = widgetUrl;
    iframe.allow = "microphone";
    iframe.setAttribute("allowtransparency", "true");
    iframe.setAttribute("scrolling", "no");

    var frameStyles = {
      position: "fixed",
      top: "auto",
      left: "auto",
      right: EDGE_GAP + "px",
      bottom: EDGE_GAP + "px",
      width: CLOSED_SIZE.width + "px",
      height: CLOSED_SIZE.height + "px",
      border: "0",
      background: "transparent",
      colorScheme: "normal",
      display: "block",
      overflow: "hidden",
      zIndex: "2147483647",
      transition: "width 180ms ease, height 180ms ease, bottom 180ms ease",
    };

    Object.keys(frameStyles).forEach(function (property) {
      iframe.style.setProperty(property, frameStyles[property], "important");
    });

    function sizeForState(state) {
      if (state !== "open") {
        return CLOSED_SIZE;
      }

      var gap = window.innerWidth <= 480 ? 8 : EDGE_GAP;
      return {
        width: Math.min(OPEN_SIZE.width, Math.max(CLOSED_SIZE.width, window.innerWidth - gap * 2)),
        height: Math.min(OPEN_SIZE.height, Math.max(CLOSED_SIZE.height, window.innerHeight - gap * 2)),
      };
    }

    function applyState(state) {
      var gap = window.innerWidth <= 480 ? 8 : EDGE_GAP;
      var size = sizeForState(state);

      iframe.dataset.state = state === "open" ? "open" : "closed";
      iframe.style.setProperty("top", "auto", "important");
      iframe.style.setProperty("left", "auto", "important");
      iframe.style.setProperty("right", gap + "px", "important");
      iframe.style.setProperty("bottom", iframe.dataset.state === "open" ? "0" : gap + "px", "important");
      iframe.style.setProperty("width", size.width + "px", "important");
      iframe.style.setProperty("height", size.height + "px", "important");
    }

    window.addEventListener("message", function (event) {
      if (event.source !== iframe.contentWindow || !event.data) {
        return;
      }

      if (event.data.type === "nic-agent-frame-state") {
        applyState(event.data.state);
      }
    });

    window.addEventListener("resize", function () {
      applyState(iframe.dataset.state);
    });

    window.NICVoiceAgent = {
      open: function () {
        applyState("open");
        iframe.contentWindow.postMessage({ type: "nic-agent-open" }, "*");
      },
      close: function () {
        applyState("closed");
        iframe.contentWindow.postMessage({ type: "nic-agent-close" }, "*");
      },
    };

    document.body.appendChild(iframe);
    applyState("closed");
  }

  if (document.body) {
    boot();
  } else {
    window.addEventListener("DOMContentLoaded", boot, { once: true });
  }
})();
