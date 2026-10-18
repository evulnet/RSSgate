# Post-mortem: the rabbit post (v0.84.0)

The longest single-bug investigation in this repo's history. One failed post
("why is this one post failing?") turned out to be **three stacked bugs, one
product-design gap, and a wake-up call for two test flakes**. Kept as a
worked example, not a changelog restatement - the *sequence* is the lesson.

Timeline: 2026-10-12 .. 2026-10-18 (live DB: article id 2673, feed
`rssblog.ameba.jp/maron0`, published 2026-10-18).

## What the user saw

"AI is not visible" (no new digested posts arriving) plus one post stuck
red: `extracted text too short (20 ch)`.

## The chain, in order of discovery

### 0. The queue was silent for a non-code reason
The LLM backend had begun serving **two** models; auto-selection requires
exactly one, so it refused everything and the outage-hold machinery
(v0.79.x) faithfully held the entire queue. Fix was operational: pin
`llm.model` in config. Lesson: "AI is down" has at least three causes
(network, model-selection, refusal) - check `/api/status`'s outage text and
one direct `curl` of the backend before reading any code.

### 1. The failed post was right, the diagnosis was nearly wrong
Diagnosing 2673, the page *appeared* to carry ~130 chars of prose - enough
that the obvious fix was "lower the floor a bit". Fetching the production
fixture and looking at WHERE the text lives killed that idea: the prose was
the **blog tagline** in the header (`h2.skinDescription`). The real post
body (`div#entryBody`) was genuinely 38 characters plus three rabbit
photos. It is a photo post.

**Lesson: reproduce against the real fixture before designing the fix.** A
reconstructed fixture would have captured the tagline as "body" and shipped
a floor-lowering that published headers as digests site-wide.

### 2. Bug #1 - the extractor only spoke `<p>`
`extract_article_text` collects `p/li/blockquote/h*/pre/td` blocks. Ameba
writes post bodies as bare text nodes + `<br>` inside plain `<div>`s
(`p-count: 0` in entryBody). So a real - if thin - post extracted as just
its title, and the root-weighting heuristic (which counts `<p>` text)
scored the body container zero. The 20-char "excerpt" was literally the
`## title` line.

Fix shape: a **fallback pass that fires only when the normal pass found
<120 chars** - leaf divs contribute their `<br>`-split lines, containers
with children contribute only their DIRECT text runs (no double counting).
Ordinary pages never take the path, which is what made it regression-safe:
the whole existing image/extract suite passed untouched the first run.

**Lesson: "no text" is a claim about your collector, not about the page.**
The cheapest extractor bug class is silent invisibility - it never errors,
it just returns less. (Hard-won lesson #2's cousin: tests passed for the
shape that never occurs.)

### 3. Bug #2 was hiding under Bug #1 - stacked failure
With div-text captured, the intended fix was "publish image-led when photos
exist". First hermetic test: still `status=error`, `image=NULL`. The photos
were invisible too - `AVATAR_RE`'s `user[-_/]` pattern rejected every image
because Ameba's CONTENT CDN is `/user_images/`. The post looked contentless
because two independent filters had each blinded one sense.

**Lesson: when the fallback path also fails, suspect stacking, not one
bug.** If fixing "no text" doesn't make "publish with images" work, the
second failure is its own bug with its own root cause - don't re-litigate
the first. (Also: this is the same word-boundary trap as the earlier
CONTENTISH fix - a junk-looking word inside a real word's territory. Junk
regexes are camelCase-word-boundary-safe by design, and `user_images` was
the exception that proved it.)

### 4. The real bug was the STATUS, not the code
Even with both senses fixed: a 16-word, 3-photo post is under every digest
floor, and the pipeline's answer was `error`. That was wrong at the product
level. A photo post is not a failure; nothing is broken and no retry will
help - the site is *finished*. So: `_publish_image_led` publishes it as-is
(longest available text becomes the summary, zero tokens,
`digest_source='image'`, a 📷 chip explains itself, and the page-upgrade
ladder can still promote it to a real digest if words ever appear).

The junk-guard question - "won't this publish cookie banners?" - answers
itself: a stub has **neither** photos **nor** words. The photo requirement
is the guard; no new setting was needed.

**Lesson: `error` must mean "retrying can help".** A terminal state that
fires on content that merely disagrees with your floors is a missing
status, not a missing check.

### 5. Fixing the LLM woke two dormant test races
- **queue panel**: read the live `/api/workqueue` twice - once for the
  assertion, once via the panel. Harmless while the queue was dead for
  days; the moment my LLM fix made the queue MOVE, the scheduler could
  claim a post between the reads and the panel legitimately said "running
  just now" where the test demanded idle. Fixed by fetching ONE snapshot,
  route-stubbing the panel to it, and asserting what the snapshot shows
  (in-flight included).
- **photo-post chip**: passed alone, failed in tier order. The sidebar
  re-renders on a 2.5 s debounce; a click landing on a replaced `li` left
  the stream unscoped, and shared-tier noise put the card past page 1.
  Fixed with click-until-provably-scoped (assert every visible card's
  `data-feed` is yours AND your card is among them), not with a longer
  sleep.

**Lessons (both already rules, now with teeth):**
- A test that asserts global live state races anything that makes the
  system move - **including the fix you just shipped**. Green for weeks
  under a dead queue was luck.
- Isolation-green + order-red is a state/ordering bug, always. Two
  consecutive full-tier runs are the receipt, not the verdict you hoped for.

### 6. Self-inflicted, and the harness caught it
- The chip test's own `wait_for_function` had `}}` from f-string habit in a
  non-f string segment → the page logged `Unexpected token '}'`. The
  `assert pg.errors == []` convention is what surfaced it. Keep that
  assertion on every browser test; it catches YOUR test code too.
- `python -c "import tests.test_release"` printed "ok" while the CHANGELOG
  format was actually broken - importing a test module RUNS nothing (and
  the earlier pytest run had been BEFORE the changelog was written). The
  guard is only a guard when pytest runs it after the write. (AGENTS
  hard-won lesson #16.)
- `pytest.ini` already sets `addopts = -q`; passing `-q` again double-quiets
  until the summary line vanishes, which silently broke a
  `pytest | grep passed` chain mid-release. Check `pytest.ini` before
  scripting on pytest output.
- One draft assertion read `assert llm.calls == 0 and not any(...) is
  False` - mangled by mid-writing. Re-read new tests before running them;
  a red test you cannot explain is a lying oracle.

## What actually verified it

Not just tests: the live scheduler, on the restarted v0.84.0 server,
published the requeued 2673 itself - `digest_source=image`, hero
`fcf3c81b….jpg` served at HTTP 200, `llm_ms=0`, card present in
`/api/articles`. The first real-world exercise of the feature was the very
post that motivated it.

## If you hit a similar bug

1. Fetch the production fixture; locate the content in the DOM before
   touching floors/filters.
2. Ask what the pipeline *should do* with this content when it is complete
   and never changes. If the answer isn't `error`, you're adding a status,
   not a check.
3. Fix invisibility bugs one sense at a time (text vs images) and prove
   each independently - stacked failures hide the second behind the first.
4. Make fallback publishing self-guarding (junk fails the new path for
   free) instead of adding settings.
5. After any fix that restores system behavior, expect dormant test races
   to fire; freeze shared state in those tests (snapshot + route stub),
   don't extend their timeouts.
6. Run your release guards as tests, after the writes, every time.
