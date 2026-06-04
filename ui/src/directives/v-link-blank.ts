import { DirectiveBinding } from "vue";

export default {
  mounted(el: HTMLElement, _: DirectiveBinding) {
    const links = el.querySelectorAll("a");
    links.forEach((link) => {
      link.setAttribute("target", "_blank");
    });
  },
  updated(el: HTMLElement, _: DirectiveBinding) {
    const links = el.querySelectorAll("a");
    links.forEach((link) => {
      link.setAttribute("target", "_blank");
    });
  },
};
