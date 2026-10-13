/* Mapenzi ya 30 scoring. Runs in the browser only: nothing is sent anywhere.
 * DRAFT rubric: needs Brave's clinical sign-off before real use.
 *
 * Points (higher = more strain), from the option values in the form:
 *   energy 0-3, sleep 0-3, support wanted 0-3, "kept you up" 0-2 (capped),
 *   connection 0-4 (from the 0-10 scale below). Max total 15.
 * Total -> score:  0-2 => 1, 3-5 => 2, 6-8 => 3, 9-11 => 4, 12+ => 5
 * Safety net: if the free-text answers mention self-harm, score is 5.
 * (Keyword matching is NOT detection; crisis resources are always shown anyway.)
 */
(function (root) {
  var BANDS = [2, 5, 8, 11]; // upper bound of score 1,2,3,4; above => 5
  var KEPT_UP_CAP = 2;
  var CRISIS_WORDS = [
    "suicide", "suicidal", "kill myself", "end my life", "want to die",
    "wanna die", "better off dead", "self harm", "self-harm", "hurt myself",
    "kujiua", "kujidhuru", "nataka kufa"
  ];

  function connectionPoints(n) {
    if (n >= 8) return 0;
    if (n >= 6) return 1;
    if (n >= 4) return 2;
    if (n >= 2) return 3;
    return 4;
  }

  function mentionsCrisis(text) {
    var t = String(text || "").toLowerCase();
    for (var i = 0; i < CRISIS_WORDS.length; i++) {
      if (t.indexOf(CRISIS_WORDS[i]) !== -1) return true;
    }
    return false;
  }

  /* a = {energy, sleep, support, connection (0-10), keptUp: [points...], text: "..."} */
  function computeScore(a) {
    if (mentionsCrisis(a.text)) return 5;
    var keptUp = Math.min((a.keptUp || []).reduce(function (s, v) { return s + v; }, 0), KEPT_UP_CAP);
    var total = (a.energy || 0) + (a.sleep || 0) + (a.support || 0) +
      connectionPoints(Number(a.connection)) + keptUp;
    for (var i = 0; i < BANDS.length; i++) {
      if (total <= BANDS[i]) return i + 1;
    }
    return 5;
  }

  var api = { computeScore: computeScore, connectionPoints: connectionPoints };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.MapenziScoring = api;
})(typeof window !== "undefined" ? window : this);
