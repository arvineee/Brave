/* Check-in form behaviour. Runs on the student's phone.
 * - scores the answers (scoring.js) and opens /response?score=N
 * - sends ONE anonymous summary to /api/checkin: multiple-choice answers and score only.
 *   The free-text reflections are never sent. A contact is sent only if the student
 *   typed one AND ticked consent.
 * - extra questions added by staff (not scored) are sent as multiple-choice answers only.
 * - the response page always opens, even if the network call fails or is slow.
 */
(function () {
  var form = document.getElementById("checkin-form");
  if (!form) return;

  var $ = function (id) { return document.getElementById(id); };
  var wantsSupport = (form.getAttribute("data-wants-support") || "").split("|");
  var q7 = $("q7"), q8 = $("q8"), q9 = $("q9");
  var contact = $("q10"), consent = $("consent");
  var boxes = form.querySelectorAll("input[data-label]");

  function label(sel) { return sel.options[sel.selectedIndex].text.trim(); }
  function num(sel) { return Number(sel.value) || 0; }

  // "What kept you up": at least one box must be ticked.
  function syncKeptUp() {
    var any = false, i;
    for (i = 0; i < boxes.length; i++) if (boxes[i].checked) any = true;
    for (i = 0; i < boxes.length; i++) boxes[i].required = !any;
  }

  // Questions 8 and 9 are needed when the student wants support or leaves a contact.
  // A contact also needs consent. The contact box itself is always visible.
  function sync() {
    var wants = q7.selectedIndex > 0 && wantsSupport.indexOf(label(q7)) !== -1;
    var hasContact = contact.value.trim() !== "";
    q8.required = q9.required = wants || hasContact;
    consent.required = hasContact;
  }

  // Extra "pick any" questions marked required: at least one box must be ticked.
  var cqGroups = form.querySelectorAll("[data-cq-group]");
  function syncCustom() {
    for (var g = 0; g < cqGroups.length; g++) {
      var id = cqGroups[g].getAttribute("data-cq-group");
      var cbs = form.querySelectorAll('input[data-cq-box="' + id + '"]');
      var any = false, k;
      for (k = 0; k < cbs.length; k++) if (cbs[k].checked) any = true;
      var need = cqGroups[g].getAttribute("data-required") === "1" && !any;
      for (k = 0; k < cbs.length; k++) cbs[k].required = need;
    }
  }

  function collectCustom() {
    var out = {}, n = 0, i, k;
    var sels = form.querySelectorAll("select[data-cq]");
    for (i = 0; i < sels.length; i++) {
      if (sels[i].selectedIndex > 0) {
        out[sels[i].getAttribute("data-cq")] =
          sels[i].getAttribute("data-kind") === "scale" ? Number(sels[i].value) : sels[i].value;
        n++;
      }
    }
    for (i = 0; i < cqGroups.length; i++) {
      var id = cqGroups[i].getAttribute("data-cq-group");
      var cbs = form.querySelectorAll('input[data-cq-box="' + id + '"]');
      var picked = [];
      for (k = 0; k < cbs.length; k++) if (cbs[k].checked) picked.push(cbs[k].getAttribute("data-cq-label"));
      if (picked.length) { out[id] = picked; n++; }
    }
    return n ? out : null;
  }

  form.addEventListener("change", function () { syncKeptUp(); sync(); syncCustom(); });
  contact.addEventListener("input", sync);
  syncKeptUp(); sync(); syncCustom();

  form.addEventListener("submit", function (e) {
    e.preventDefault();
    var radio = form.querySelector('input[name="connection"]:checked');
    var kept = [], keptPts = [], i;
    for (i = 0; i < boxes.length; i++) {
      if (boxes[i].checked) {
        kept.push(boxes[i].getAttribute("data-label"));
        keptPts.push(Number(boxes[i].getAttribute("data-points")) || 0);
      }
    }
    var connection = radio ? Number(radio.value) : 5;
    var score = window.MapenziScoring.computeScore({
      energy: num($("q1")), sleep: num($("q3")), support: num(q7),
      connection: connection, keptUp: keptPts,
      text: $("q5").value + " " + $("q6").value   // used only on this device
    });

    var payload = {
      score: score, energy: label($("q1")), sleep: label($("q3")), connection: connection,
      kept_up: kept, support_now: label(q7)
    };
    if (q8.selectedIndex > 0 && q9.selectedIndex > 0) {
      payload.support_type = label(q8);
      payload.channel = label(q9);
    }
    var extra = collectCustom();
    if (extra) payload.custom = extra;
    var c = contact.value.trim();
    if (c && consent.checked) payload.request = { contact: c, consent: true };

    var btn = $("submit-btn");
    btn.disabled = true;
    btn.textContent = "Preparing your response…";

    var url = form.getAttribute("data-response-url") + "?score=" + score;
    var gone = false;
    function go() { if (!gone) { gone = true; window.location.assign(url); } }
    try {
      fetch(form.getAttribute("data-api-url"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
        keepalive: true
      }).then(go, go);
    } catch (err) { go(); }
    setTimeout(go, 2500);
  });
})();
