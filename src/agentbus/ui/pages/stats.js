import { h, registerPage } from "../app.js";

registerPage("stats", { title: "Статистика", render });

function render(root) {
  root.replaceChildren(
    h("h1", {}, "Статистика"),
    h("p", { class: "muted" }, "скоро — T7"),
  );
}
