import { h, registerPage } from "../app.js";

registerPage("mailboxes", { title: "Ящики", render });

function render(root) {
  root.replaceChildren(
    h("h1", {}, "Ящики"),
    h("p", { class: "muted" }, "скоро — T7"),
  );
}
