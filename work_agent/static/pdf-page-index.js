// A URL fragment stays in the browser. Resolve an older line-only citation
// through the visible page index before opening its original physical page.
function openCitedPage() {
  const match = /^#L([1-9][0-9]{0,6})$/.exec(location.hash);
  if (!match) return;
  const line = Number(match[1]);
  const pageLinks = document.querySelectorAll(".pdf-page-index a[data-first-line]");
  for (const link of pageLinks) {
    if (line >= Number(link.dataset.firstLine) && line <= Number(link.dataset.lastLine)) {
      location.replace(link.href + location.hash);
      return;
    }
  }
}
openCitedPage();
window.addEventListener("hashchange", openCitedPage);
