(() => {
  "use strict";

  const root = document.documentElement;
  const folioBase = root.dataset.folioBase || "";
  const folioPath = (path) => `${folioBase}${path}`;
  const savedTheme = localStorage.getItem("folio-theme");
  if (savedTheme === "light" || savedTheme === "dark") {
    root.dataset.theme = savedTheme;
  }

  document.querySelectorAll("[data-theme-toggle]").forEach((button) => {
    button.addEventListener("click", () => {
      const current =
        root.dataset.theme ||
        (window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
      const next = current === "dark" ? "light" : "dark";
      root.dataset.theme = next;
      localStorage.setItem("folio-theme", next);
    });
  });

  document.querySelectorAll("[data-copy-code]").forEach((button) => {
    button.addEventListener("click", async () => {
      const block = button.closest(".code-block");
      const code = block ? block.querySelector("code") : null;
      if (!code) return;
      const original = button.textContent;
      try {
        await navigator.clipboard.writeText(code.textContent || "");
        button.textContent = "Copied";
      } catch (_error) {
        const selection = window.getSelection();
        const range = document.createRange();
        range.selectNodeContents(code);
        selection.removeAllRanges();
        selection.addRange(range);
        button.textContent = "Selected";
      }
      window.setTimeout(() => {
        button.textContent = original;
      }, 1400);
    });
  });

  document.querySelectorAll("[data-category-form]").forEach((form) => {
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const input = form.querySelector('input[name="category"]');
      const button = form.querySelector('button[type="submit"]');
      const status = form.querySelector("[data-category-status]");
      const categoryLabel = document.querySelector("[data-category-label]");
      const entryId = form.dataset.entryId;
      const category = input ? input.value.trim() : "";
      if (!entryId || !category || !button || !status) return;

      button.disabled = true;
      status.textContent = "Moving…";
      try {
        const response = await fetch(
          folioPath(`/api/entry/${encodeURIComponent(entryId)}/category`),
          {
            method: "POST",
            headers: {
              "Content-Type": "application/json",
              "X-Folio-Request": "1",
            },
            body: JSON.stringify({ category }),
          },
        );
        const result = await response.json();
        if (!response.ok) {
          throw new Error(result.error || "Could not move this response.");
        }
        input.value = result.category;
        if (categoryLabel) categoryLabel.textContent = result.category;
        status.textContent =
          result.status === "unchanged"
            ? `Already in ${result.category}.`
            : `Moved to ${result.category}.`;
      } catch (error) {
        status.textContent =
          error instanceof Error ? error.message : "Could not move this response.";
      } finally {
        button.disabled = false;
      }
    });
  });

  document.querySelectorAll("[data-add-entry]").forEach((form) => {
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const button = form.querySelector('button[type="submit"]');
      const status = form.querySelector("[data-add-entry-status]");
      const markdown = form.querySelector('textarea[name="markdown"]');
      const category = form.querySelector('input[name="category"]');
      const title = form.querySelector('input[name="title"]');
      const agent = form.querySelector('input[name="agent"]');
      if (!button || !status || !markdown || !category || !title || !agent) return;

      const payload = {
        markdown: markdown.value,
        category: category.value.trim(),
        title: title.value.trim(),
        agent: agent.value.trim(),
      };
      if (!payload.markdown.trim() || !payload.category || !payload.agent) return;

      button.disabled = true;
      status.textContent = "Saving…";
      try {
        const response = await fetch(folioPath("/api/entry"), {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-Folio-Request": "1",
          },
          body: JSON.stringify(payload),
        });
        const result = await response.json();
        if (!response.ok) {
          throw new Error(result.error || "Could not save this response.");
        }
        window.location.assign(folioPath(result.reader_path));
      } catch (error) {
        status.textContent =
          error instanceof Error ? error.message : "Could not save this response.";
        button.disabled = false;
      }
    });
  });

  document.querySelectorAll("[data-delete-category]").forEach((form) => {
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const button = form.querySelector('button[type="submit"]');
      const status = form.querySelector("[data-delete-category-status]");
      const categoryId = form.dataset.categoryId;
      const categoryName = form.dataset.categoryName || "this category";
      const entryCount = Number.parseInt(form.dataset.entryCount || "0", 10);
      if (!categoryId || !button || !status) return;

      const entryMessage =
        entryCount === 1
          ? "Its response will be moved to Inbox."
          : `Its ${entryCount} responses will be moved to Inbox.`;
      if (!window.confirm(`Delete “${categoryName}”? ${entryMessage}`)) return;

      button.disabled = true;
      status.textContent = "Deleting…";
      try {
        const response = await fetch(
          folioPath(`/api/category/${encodeURIComponent(categoryId)}/delete`),
          {
            method: "POST",
            headers: {
              "Content-Type": "application/json",
              "X-Folio-Request": "1",
            },
            body: "{}",
          },
        );
        const result = await response.json();
        if (!response.ok) {
          throw new Error(result.error || "Could not delete this category.");
        }
        window.location.assign(folioPath(result.library_path || "/library"));
      } catch (error) {
        status.textContent =
          error instanceof Error ? error.message : "Could not delete this category.";
        button.disabled = false;
      }
    });
  });

  document.querySelectorAll("[data-delete-entry]").forEach((form) => {
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const button = form.querySelector('button[type="submit"]');
      const status = form.querySelector("[data-delete-entry-status]");
      const entryId = form.dataset.entryId;
      const entryTitle = form.dataset.entryTitle || "this response";
      if (!entryId || !button || !status) return;
      if (
        !window.confirm(
          `Delete “${entryTitle}”? This permanently removes the saved response.`,
        )
      ) {
        return;
      }

      button.disabled = true;
      status.textContent = "Deleting…";
      try {
        const response = await fetch(
          folioPath(`/api/entry/${encodeURIComponent(entryId)}/delete`),
          {
            method: "POST",
            headers: {
              "Content-Type": "application/json",
              "X-Folio-Request": "1",
            },
            body: "{}",
          },
        );
        const result = await response.json();
        if (!response.ok) {
          throw new Error(result.error || "Could not delete this response.");
        }
        window.location.reload();
      } catch (error) {
        status.textContent =
          error instanceof Error ? error.message : "Could not delete this response.";
        button.disabled = false;
      }
    });
  });

  document.querySelectorAll("[data-print]").forEach((button) => {
    button.addEventListener("click", () => window.print());
  });
})();
