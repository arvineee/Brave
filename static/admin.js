// Staff pages only. The Print buttons call the browser's print dialog, which can save as PDF.
// (Kept in a file because the site's content policy blocks inline scripts.)
document.addEventListener("click", function (e) {
  var btn = e.target.closest ? e.target.closest("[data-print]") : null;
  if (btn) {
    window.print();
  }
});
