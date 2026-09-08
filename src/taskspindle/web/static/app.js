(function () {
  "use strict";
  import("/static/shell.js")
    .then(({ start }) => start())
    .catch((error) => {
      console.error("TaskSpindle failed to start", error);
      const app = document.getElementById("app");
      if (app) app.textContent = "The operator console could not start. Reload to try again.";
    });
})();
