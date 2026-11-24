import { h, registerPage } from "../app.js";

registerPage("history", { title: "История", render });

function render(root) {
  root.replaceChildren(
    h("h1", {}, "История"),
    h("p", { class: "muted" }, "скоро — T7"),
  );
}
