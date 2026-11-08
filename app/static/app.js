document.addEventListener("DOMContentLoaded", () => {
  const flash = document.querySelector(".flash");
  if (flash) {
    window.setTimeout(() => {
      flash.style.transition = "opacity .45s ease, transform .45s ease";
      flash.style.opacity = "0";
      flash.style.transform = "translateY(-6px)";
      window.setTimeout(() => flash.remove(), 500);
    }, 9000);
  }

  document.querySelectorAll("details.edit").forEach((details) => {
    details.addEventListener("toggle", () => {
      if (details.open) {
        details.scrollIntoView({ block: "nearest", behavior: "smooth" });
      }
    });
  });

  document.querySelectorAll("form[data-confirm]").forEach((form) => {
    form.addEventListener("submit", (event) => {
      if (!window.confirm(form.dataset.confirm)) {
        event.preventDefault();
      }
    });
  });
});
