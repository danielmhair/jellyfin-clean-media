# PRD — VidAngel-Style Filter Taxonomy

> **Status: not started.** Created 2026-08-29.
> ✅ implemented and verified · ⏳ in progress · ⬜ not started.
>
> Extends [clean-media-prd.md](clean-media-prd.md) and builds directly on the
> worker-owned settings store shipped in
> [2026-08-14-queue-manager-and-in-plugin-settings.md](2026-08-14-queue-manager-and-in-plugin-settings.md)'s
> successor work (`worker/settings.py`, the plugin's Settings/Advanced tabs,
> both editable live with no worker restart).
>
> **Naming note:** [2026-08-25-distributed-workers-and-resilience.md](2026-08-25-distributed-workers-and-resilience.md)
> (not started) separately proposes an "internal supervisor" and "external
> watchdog" for distributed job resilience. That is unrelated to
> `worker/supervisor.py`, the small always-on helper already shipped this
> week that lets the plugin start/restart the worker process. Same word,
> two different things — do not conflate them.
>
> **Scope note:** an earlier draft of this PRD trimmed VidAngel's taxonomy
> down to a simplified subset. That draft was wrong to do that — **every
> category and subcategory VidAngel publishes is in scope and is
> represented below.** Nothing is dropped. Where VidAngel's own text
> matters mechanically (a rule that genuinely needs meaning, not just a
> word match, or a rule that only applies to certain film ratings), that is
> called out explicitly in [Implementation Decisions](#implementation-decisions)
> as *how* it gets built and in what phase — never as a reason to omit it
> from the taxonomy itself.

---

## Problem Statement

Clean Media detects far less than a family actually wants filtered, and what
it does detect can't be tuned without editing Python and redeploying.

Today's word-based detection (`worker/engines/profanity.py`) is three flat
sets — `STRONG_WORDS`, `MILD_WORDS`, `BLASPHEMY_WORDS` — with two on/off job
options and one free-text add-list. Racial slurs are mixed into the same set
as ordinary profanity. Words that VidAngel treats as childish rather than
blasphemous (`OMG`, `geez`, `gosh`) aren't distinguished from words it treats
as genuinely blasphemous. There is no way to turn a single word off.

Today's visual detection (`worker/policy.py`'s eight `OBSERVATIONS` fields)
covers nudity, sexual activity, kissing, and suggestive framing — and
nothing else. No violence, no drugs/alcohol, no immodesty (bikinis,
cleavage, short shorts), no disturbing imagery, no vulgar gestures. A film
with graphic violence and zero nudity sails through analysis with nothing
flagged.

The administrator has no vocabulary for any of this beyond "detected / not
detected." VidAngel's own published filter guide — reproduced in full below
— shows what a mature version of this looks like: every category named,
defined precisely (including which ratings a rule applies to, and explicit
exclusions), with per-item control within categories that are lists of
things (words) rather than one blunt toggle.

## Solution

Rebuild both detection surfaces around VidAngel's full category taxonomy —
every category, every subcategory, nothing trimmed — with per-word control
where VidAngel itself offers it, and category/subcategory-level control
everywhere else.

The taxonomy below is the reference. Every entry in it gets a home in the
settings schema and the plugin's new **Filters** tab. Not every entry is
buildable the same way — some are a straightforward word list, some need a
new VLM field, a few genuinely need context or a film's rating (neither of
which this project has a concept of today) to match VidAngel exactly, and
one (Credits) isn't a content-judgment problem at all. Implementation
Decisions tags each entry with its mechanism and phase so that distinction
is explicit and honest, not a silent scope cut.

---

## Filter Taxonomy (complete — nothing trimmed)

### LANGUAGE

**PROFANITY** — *All ratings unless noted.*
- Word list: `mofo` (filtered as the f-word), `fu` (the abbreviation, not
  spelled out), `screw`, `douche`, `douchebag`, `prick`/`pricks`,
  `cunt`/`cunts`, `twat`/`twats`, `skank`/`skanks`, `dick`/`dicks`/`dickhead`,
  `cock`/`cocks`, `pussy`/`pussies`, `fuck`/`fucker`/`fucking`/`fucked`/
  `motherfucker`/`motherfucking`, `shit`/`shitty`/`bullshit`/`horseshit`/
  `shithead`, `asshole`/`assholes`, `bitch`/`bitches`/`bitching`,
  `bastard`/`bastards`, `ass`/`asses`, `goddamn`/`goddamnit`/`goddammit`
  (also filed under Blasphemy — VidAngel: "each filter covers the entire
  phrase"), `damn`/`damnit`/`dammit`, `jackass`/`dumbass`/`badass`,
  `piss`/`pissed`/`pissing`.
- If any of the above (screw, douche, prick, cunt, twat, skank, dick, cock,
  pussy in particular) is used in a sexual way, it *also* gets a filter
  option under Sexual References.
- `hell` is filtered only when used as profanity, not when the scene is
  literally discussing Heaven and Hell.
- G/PG: `BS` is filtered, but under Childish/Crude Language, not here.

**BLASPHEMY** — *All ratings unless noted.*
- Word list: `god`/`gods`, `jesus`, `christ`, `jesu`, `goddamn`/`goddamnit`/
  `goddammit` (dual-filed with Profanity — filtering it here catches the
  whole phrase).
- `God`, `Jesus`, etc. are **not** filtered when not used in a derogatory
  manner.
- G/PG: `OMG`, `geez`, `gosh` are filtered, but under Childish/Crude
  Language, not here.

**CHILDISH/CRUDE LANGUAGE** — generally, things you wouldn't want a 3-year-old
repeating.
- Word list: `bs`, `omg`, `geez`, `gosh`, `jeez`, `stupid`, `idiot`/`idiots`/
  `idiotic`, `dumb`, `dummy`, `moron`/`morons`/`moronic`, `jerk`/`jerks`,
  `loser`/`losers`, `poop`/`poopy`, `fart`/`farted`/`farting`, `butt`/`butts`,
  `butthole`/`buttholes`, `freak`/`freaks`, `sucks`, `hate`, `shutup`
  (today's build already matches this as a single normalized token; see the
  mean-vs-plain-expression caveat below).
- All ratings: **not** filtered — `darn`, `heck`, `shoot`, `heaven's sake`,
  `lands sake`, `what the` (no profanity intended). Also **not** filtered:
  `tit`/`boob`/`balls`/`penis`/`vagina` etc. when not used sexually (when
  used sexually, they belong under Sexual References instead).
- G/PG only: `crap`, `fart`, `poop`, `effin'`, `freakin'`, `frickin'` are
  filtered; insults like `stupid`, `idiot`, `butthole` are filtered; `shut
  up` is filtered only when used meanly/hurtfully, not as a plain
  expression.
- PG-13/R/TV-MA: `crap`, `friggin'`, `frickin'` are **not** filtered.

**RACIAL SLURS AND BIGOTED LANGUAGE** — racist, sexist, and/or discriminatory
language in any form.
- The n-word is filtered **regardless of how it's used** — no context
  exemption, unlike almost every other category here.
- Word list carried over from today's build: `nigger`/`niggers`/`nigga`,
  `faggot`/`faggots`/`fag`, `retard`/`retarded`. This category is
  intentionally extensible — broader discriminatory-language coverage
  should be curated deliberately over time rather than speculatively
  enumerated in this document.

**SEXUAL REFERENCES AND INNUENDOS** — references or jokes about sex,
flirting, innuendos, etc.
- Word list: `porno`, `pornography`, `prostitute`/`prostitutes`,
  `slut`/`sluts`, `whore`/`whores`.
- Talk about sexually transmitted diseases.
- G/PG: references to affairs, someone cheating, or divorce.
- G/PG/PG-13 only: people saying someone is "hot" or "sexy." **Not**
  filtered in R/TV-MA content.

**CAPTIONS WITH PROFANITY** — VidAngel's own guide has no rule content under
this heading beyond the title. It is not a distinct detection surface: it's
VidAngel's term for profanity that reaches the viewer via captions, which
for Clean Media is exactly what the `subtitles` engine already reads. No new
work; noted here only so the full taxonomy has a place for it.

### SEX

Baseline: if people are clothed and lying on a bed talking, that alone is
**not** filtered unless it's immediately pre- or post-sex. Homosexual and
heterosexual relations use the same guidelines throughout.

- **SEXUALLY SUGGESTIVE** — behaviors with action or sexual undertones
  enticing or implying sexual intent; any signage for sex (adult stores,
  movie theaters, etc.); people ogling, gawking, staring lustfully, or
  whistling at another person; sex toys not shaped like genitalia/body
  parts (those go under Shown with Nudity instead).
- **IMPLIED SEX** — sex happening off-screen, or immediately before/after;
  includes kissing with touching and/or removal of clothes.
- **SHOWN WITHOUT NUDITY** — sex shown via a discreet camera angle, under
  bedsheets, etc.; includes animals, robots, etc. (VidAngel does not
  consider animals nude).
- **SHOWN WITH NUDITY** — sex shown with any body part that would normally
  be covered by a bikini or Speedo; anyone wearing/using sex toys shaped
  like nude body parts.
- **SEXUAL ASSAULT** — rape, attempted rape, bestiality, etc.; references
  to rape or molestation. A scene here also gets Nudity/Immodesty filters
  offered if the scene contains them.

### NUDITY

- **NUDITY (WITHOUT SEX)** — skinny dipping, bathing, flashing, mooning,
  etc.
- **STATUES AND PAINTINGS** — an explicit exclusion, not a filter: art,
  statues, mannequins, drawings, stained glass, reliefs, etc. are not
  filtered for nudity if breasts or genitals are not visible. (Already
  matches this project's existing VLM calibration — "ignore statues,
  sculptures, mannequins, paintings, cartoons, toys" — no new work.)
- **IMPLIED NUDITY** — not wearing clothing but private areas are not
  shown. A person covered by a bath towel or sheet is not filtered here
  unless cleavage or other immodesty is visible, in which case it's an
  Immodesty filter instead.
- **FEMALE AND MALE NUDITY** — any body part that would be covered by a
  bikini or Speedo.

### KISSING

- **NORMAL** — lip-to-lip kissing. Platonic kisses on the hand, cheek,
  forehead, or in greeting are **not** filtered.
- **PASSIONATE** — French kissing, making out, or sensually kissing parts
  of the body. If it involves touching or removal of clothes, it moves
  under the Sex category instead.

### IMMODESTY

Baseline: top of the butt crack, buttock curves or close-ups, and very
short mini skirts/shorts that reveal buttocks.

- **FEMALE** — bikinis and revealing one-piece swimsuits; focused chest
  shots or overflowing cleavage; bare midriffs *when* worn with a bra-like
  top/sports bra (a bare midriff alone is not, by itself, a trigger).
  **Not** filtered: natural cleavage; backless dresses, unless low enough to
  show the butt crack. Period pieces, or films where immodesty is
  pervasive throughout, get a volume/frequency judgment call rather than a
  crisp per-instance rule (VidAngel: "uses data and research to determine
  the level/number of immodesty filters offered"). G/PG only: cheerleading
  or ballerina outfits when panties/undershorts that look like underwear
  are visible.
- **MALE** — form-fitting underwear, briefs, and swimsuits (Speedo).
  Shirtless men are **not** filtered here unless sexually suggestive
  (already the existing `male_shirtless` + `sexualised_framing` behavior).

### VIOLENCE

- **IMPLIED** — the violence itself isn't shown on screen. All ratings:
  graphic descriptions/details of a violent act; detailed talk of suicide.
  G/PG: threatening language.
- **NON-GRAPHIC** — violence with no blood. All ratings: punching, kicking,
  falling, shooting, stabbing, etc., as long as no blood is visible;
  protruding-but-not-through objects (swords, etc.) with no blood shown;
  bones breaking if only heard, not seen. G/PG: intentional
  aggression/terror, people hanging from moving vehicles/helicopters/
  ledges (not injured), a knife held to someone's throat.
- **GRAPHIC** — violence with blood or breaking bones. Bloody injuries,
  splatter, spraying blood, bloody bodies, bloody dead bodies; bodies
  impaled all the way through; burning bodies; bloody objects if dripping
  or caked with blood.
- **GORE** — gore, bloody guts, bloody severed body parts. Disembowelment;
  all decapitations; all actual severing of body parts (including in the
  background); weapons/bullets causing excessive graphic splatter,
  shattered skulls, brain matter. **Not** filtered: slaughtered/hanging
  meat for food, unless extremely gross.
- **DISTURBING IMAGES** — extremely gross scars; a living person with an
  object protruding from them with no violence currently happening and no
  blood; severed body parts (seen after the initial severing) with no
  blood; mass graves of bodies, bodies burning on pyres; dead bodies with
  arrows/objects sticking out with no blood; anything involving children/
  young teens' unnatural deaths. **Not** filtered: unconscious people.
- **ANIMAL VIOLENCE** — not a separate top-level category: animal abuse or
  violence is filed under Violence itself, with the finding's own
  description noting it's animal-related.

### ALCOHOL & DRUGS

- **IMPLIED USE** — all ratings: underage talk/handling of smoking, drugs,
  and alcohol, if detailed or promoting use. G/PG/PG-13: all discussion,
  handling, making, and visibility of illegal drugs; any signage
  indicating illegal drugs; unlit cigarettes/cigars in someone's mouth.
- **LEGAL USE** — G/PG/PG-13/TV-14: consumption only. R-rated/TV-MA: **not**
  filtered at all for alcohol and tobacco.
- **ILLEGAL USE** — all ratings: prescription medication is **not** filtered
  unless it's being abused or used recreationally. G/PG/PG-13/TV-14:
  consumption of illegal drugs and underage drinking/smoking, including in
  the background. R-rated/TV-MA: consumption only.

### OTHER ELEMENTS

- **CREDITS** — opening credits, closing credits, episode recap/outtakes,
  advertisements. Structurally different from every other row in this
  taxonomy: this is a "skip this segment" problem, not a "judge this
  content" problem, so it does not belong on the VLM's per-frame call at
  all — see Implementation Decisions.
- **VULGAR GESTURES** — includes, but is not limited to: crotch-grabbing,
  gestures for profanities, people/animals mimicking any sex act.
- **OBJECTIONABLE/DISTURBING/SCARY** — all ratings: seizures, tagged only
  if violent, someone has to restrain the person, or they hurt themselves;
  someone handling a condom (packaged or not) in a non-sexual scene;
  someone talking about killing themselves/suicide and/or self-inflicted
  pain, cutting, etc.; a visible tattoo needle on or entering skin.
  **Not** filtered unless something derogatory is said or done: swastikas
  and Confederate flags. **Not** filtered unless blood or flesh is shown:
  skeletons or medical drawings. G/PG: friendly ghosts are **not** filtered
  unless they become frightening or do something gross; situations
  frightening or saddening to the character are tagged; skeletons and
  skulls (non-medical, non-normal drawings) **are** tagged.
- **HUMAN FUNCTIONS/MEDICAL** — conversations about life events, bodily
  functions, etc.
  - *Life events*: death by natural/non-violent causes; female life
    events; birth/labor/contractions filtered only if something is shown
    or once birth actually starts; the process of dying is **not**
    filtered unless very disturbing or it involves a child/teenager.
  - *Bodily functions/jokes*: gross bodily fluids/functions (e.g. passing
    gas); nosebleeds not caused by violence; potty talk.
  - *Medical – graphic*: medical procedures where blood, organs, or
    anything gross is shown.
  - *Medical – procedures*: vaccines/shots where the needle penetrates the
    skin; doctor procedures actually shown that do not include blood.

---

## Carrying forward what already works

Cross-checked against the current build (`worker/policy.py`,
`worker/engines/vlm_engine.py`, `worker/engines/profanity.py`) so none of
its hard-won lessons get quietly lost in the rewrite. These are binding on
the new work, not background reading:

- **Observe primitives, classify in code — mandatory for every tiered
  category, not just inherited by default.** `worker/policy.py`'s entire
  design exists because a direct negative instruction failed in
  production: telling the model "a shirtless man is NOT nudity" got 50
  shots of a shirtless character flagged as nudity at 0.95+ confidence,
  one of them literally described as "no explicit nudity." The fix was
  asking the model only for raw observable facts (`male_shirtless: bool`)
  and deriving the verdict in `classify()`. **Violence's four tiers,
  Alcohol & Drugs' three tiers, and Immodesty's severity judgment must
  follow the same split** — the VLM answers primitives ("is blood
  visible," "is a body part visibly severed," "is a bottle/needle/drug
  paraphernalia visible," not "is this graphic or gore"), and
  `classify()` derives the tier deterministically. Asking the model to
  emit a tier directly repeats the exact failure this architecture was
  built to avoid.
- **The "ignore what isn't real" exclusion needs a Violence/Gore
  equivalent.** Today's fixed calibration intro excludes statues,
  mannequins, paintings, cartoons, toys, and costumed/non-human creatures
  from nudity judgments — without it, a realistic statue reads as a nude
  body. Gore and graphic violence are exposed to the identical failure
  mode from a different direction: fake blood, practical-effects
  prosthetics, animation, and in-film video-game or TV-within-the-film
  footage all "look like" real violence to a frame classifier. This needs
  its own explicit exclusion in the new fields' guidance, not a hope that
  the existing nudity-scoped sentence covers it.
- **The `tentative` mechanism should cover the new judgment-call
  categories.** `is_tentative()` exists today so a soft category
  ("suggestive") reaches review without being presented as a certain
  verdict. VidAngel's own language marks several new categories the same
  way — Implied Violence, Disturbing Images, Objectionable/Disturbing/
  Scary, and period-piece Immodesty (VidAngel's own "uses data and
  research" phrasing) are judgment calls, not crisp facts, and should be
  `tentative` for the same reason `suggestive` is.
- **Two calibration sentences carry forward verbatim.** "When genuinely
  uncertain, answer false — a human reviews every true" (the per-field
  bias is safe specifically because every true positive still goes
  through review, not because false negatives don't matter) and
  "confidence scores are uncalibrated — never gate on them" (CLAUDE.md;
  a 0.98 on an ice cream cone is documented, real) both apply to the new
  fields exactly as written, not just in spirit — worth restating on each
  new field's guidance rather than assuming it's inherited from the
  shared intro alone once tier judgments are involved.
- **The negative-example house style is the template for every new
  field's guidance text.** `kissing`'s current guidance doesn't just
  define the positive case — it lists the near-misses that are false
  ("faces merely close, foreheads together, an embrace, or about-to-kiss
  is false"). New fields should be drafted the same way: the positive
  definition plus its most likely false-positive neighbor, not a bare
  definition.
- **Two mechanisms already generalize for free — don't rebuild them.**
  `_boost_if_dark()` (CLAHE contrast boost on underlit frames) runs on
  every sampled frame before any field-specific judgment, so every new
  category already benefits from it with zero additional code. `action:
  skip | blur` is already a generic per-finding field, so a new category
  offering "blur" instead of "skip" (gore, immodesty) needs no new
  plumbing, only the UI/policy decision of which categories default to
  which.
- **The audio side already proves the perceive-then-classify shape
  works.** `worker/engines/whisper_engine.py` caches the full transcript
  as `.whisper.json` specifically "so wordlist changes only re-match,
  never re-transcribe" — raw perception saved once, cheap classification
  re-run freely on top of it. [Performance/accuracy
  posture](#implementation-decisions) extends this exact, already-proven
  pattern to the visual side (a cached rich description instead of
  direct multi-field classification) rather than inventing a new shape.
- **Historical note, not a pattern to repeat:** the module still contains
  an older, unused `PROMPT` constant whose text explicitly says "Do NOT
  report violence, weapons, blood, or people who are simply clothed."
  Violence detection wasn't merely never gotten to — it was deliberately
  excluded by name in an earlier design. That old instruction is itself
  the same negative-instruction shape documented to fail for nudity, so
  it's context for why this is genuinely new territory, not a design to
  carry forward.

---

## User Stories

1. As an administrator, I want racial slurs filtered independently of
   ordinary profanity, so that I can allow mild swearing for older kids
   while still always catching slurs.
2. As an administrator, I want to turn off a single word within a category
   (e.g. allow "hell" and "damn" but keep the rest of Profanity on), so
   that the filter matches my family's actual standards instead of an
   all-or-nothing toggle.
3. As an administrator, I want to see every word a category currently
   filters, so that I know exactly what "Profanity" or "Childish Language"
   means for my library, not just its name.
4. As an administrator, I want to add my own words to any word category
   (not just a single undifferentiated extra-words bucket), so that a
   family-specific term lands in the right place and inherits that
   category's on/off state.
5. As an administrator, I want films checked for violence across all four
   severity tiers (implied, non-graphic, graphic, gore) plus disturbing
   imagery, so that a bloodless-but-brutal or gory film doesn't sail
   through analysis with zero findings the way it does today.
6. As an administrator, I want films checked for drug and alcohol use
   (implied, legal, illegal), so that I can decide per-film whether that's
   acceptable for my kids.
7. As an administrator, I want films checked for immodest dress — not just
   outright nudity — so that swimwear/cleavage-heavy scenes surface for
   review the way VidAngel's own Immodesty category does.
8. As an administrator, I want films checked for vulgar gestures and
   objectionable/disturbing/scary content, so those get review coverage
   too, not just nudity and language.
9. As an administrator, I want the Sex category broken into its own
   sub-tiers (Sexually Suggestive, Implied, Shown-without-Nudity,
   Shown-with-Nudity, Sexual Assault), so that "some kind of sexual
   content" isn't collapsed into one undifferentiated flag the way it is
   today.
10. As an administrator, I want a distinct Sexual Assault flag, so that
    rape/molestation content is identifiable as such, not folded into
    ordinary "sexual activity."
11. As an administrator, I want each new visual category and subcategory to
    have its own editable guidance text, exactly like the existing eight
    fields, so that I can tune "what counts as graphic violence" or "what
    counts as gore" for my own comfort level the same way I already can
    for nudity.
12. As an administrator upgrading from the current build, I want my
    already-saved `profanityIncludeMild`/`profanityIncludeBlasphemy`/
    `profanityExtraWords` settings to carry forward into the new per-word
    schema automatically, so that turning on this feature doesn't silently
    reset my filtering back to defaults.
13. As an administrator, I want the Filters tab organized by category (with
    Sex/Nudity/Violence/Alcohol&Drugs/Human Functions further organized by
    their subcategories) with word lists collapsed by default, so that a
    taxonomy this size doesn't turn the settings page into an unreadable
    wall of toggles.
14. As an administrator, I want a search box within the Filters tab, so
    that I can jump straight to a specific word or category instead of
    scrolling a long list.
15. As an administrator, I want the existing Advanced tab's VLM guidance
    text fields, VLM host pool, and recovery helper toggle to keep working
    exactly as they do today, so that splitting the tab doesn't regress
    anything already shipped.
16. As an administrator, I want a reset-to-default action per category, so
    that I can undo my own customization of an entire category in one
    click, matching the existing "Reset all to defaults" pattern already
    shipped for VLM guidance.
17. As an administrator reviewing findings, I want a finding to show which
    specific category/subcategory and guidance text produced it, so that I
    can judge whether the detection was reasonable — for every category in
    this taxonomy, not just today's eight.
18. As a developer, I want the word taxonomy expressed as a single deep
    module with a tiny lookup interface, so that `subtitle_engine.py` and
    `whisper_engine.py` don't need to know anything about categories,
    defaults, or overrides — only "is this word flagged right now."
19. As a developer, I want `worker/policy.py`'s `classify()` to keep its
    existing signature shape (observations in, one category out) even as
    the observation set grows substantially, so that adding a category is
    additive, not a rewrite of the classification logic.
20. As a developer, I want the new VLM fields added without changing the
    fixed calibration intro or the JSON-schema footer's *contract* (still
    always code-generated, never admin-editable), so that the "no admin
    edit can ever break the JSON parse" guarantee holds for the full field
    set, however large it grows.
21. As an administrator, I want to know when a VidAngel rule is approximated
    by a pattern heuristic rather than true language understanding (e.g.
    "hell" filtered only when profane, not when discussing the afterlife;
    "shut up" filtered every time pending a future context refinement), so
    that I understand why a handful of filters are approximate rather than
    being surprised later.
22. As an administrator running the worker on constrained hardware (e.g. an
    8GB unified-memory machine also running Jellyfin itself), I want to be
    told plainly whether enabling every new visual category meaningfully
    slows or destabilizes the visual pass, so that I can make an informed
    trade-off rather than discover it as a crash.
23. As an administrator, I want Credits (opening/closing/recap/ads) treated
    as a skip-segment feature rather than a content-safety finding, so
    that it behaves like Jellyfin's own intro-skip rather than showing up
    oddly in the findings review list alongside actual content concerns.
24. As an administrator, I want the four new word categories' defaults to
    be reasonable out of the box (Profanity on, Blasphemy off, Childish
    off, Slurs on — matching what's already shipped), so that a fresh
    install is usable without visiting the Filters tab first.
25. As an administrator, I want to create an entirely new word category of
    my own (not just toggle words in the built-in ones), so that a
    family- or region-specific term I care about gets organized the same
    way the built-in categories are, not dumped into a single flat
    extra-words list.
26. As an administrator, I want to add or remove individual words within
    any category, including built-in ones, so that the taxonomy actually
    reflects my library rather than being frozen at whatever shipped.
27. As an administrator, I want filtering to respect each film's own rating
    where VidAngel's rules are rating-dependent (profanity thresholds,
    alcohol/drug consumption, "hot"/"sexy" commentary), using the rating
    Jellyfin already has for that title, so that a G-rated film and an
    R-rated film aren't filtered identically when VidAngel itself
    wouldn't.
28. As an administrator with an unrated/unmetadata'd personal rip, I want
    filtering to default to the strictest rating tier rather than silently
    skipping rating-gated rules, so that missing metadata never means
    quietly under-filtering.
29. As an administrator, I want a brand-new category added after I've
    already analyzed my library to be applicable to already-analyzed films
    without a full GPU re-run, so that the taxonomy can keep growing
    without an ever-growing backlog of re-analysis work.
30. As an administrator, I want sex that happens off-screen (VidAngel's
    Implied Sex) to be detectable from what characters say, not just what
    the camera shows, so that a visual-only pass's blind spot doesn't
    mean this category is unfilterable in practice.
31. As a developer, I want a fixed, reusable set of real, labeled test
    clips built from already-reviewed films, so that comparing two
    candidate models or prompt strategies takes minutes against a shared
    baseline instead of re-analyzing full films for every experiment.
32. As a developer, I want every future change to guidance text or the
    taxonomy re-run against that same test set before it ships, so that a
    fix to one category's wording can't silently regress another's
    accuracy without anyone noticing until a real film gets it wrong.
33. As a developer, I want candidate detection backends (a different
    model, Ollama vs. MLX vs. a hand-rolled server) compared on accuracy
    *and* speed together against the same test set, so that a faster
    option that misses more real content isn't mistaken for a strict
    improvement.

## Implementation Decisions

Every taxonomy entry above maps to one of these mechanisms. Tagging is
explicit so nothing is silently dropped — a tag other than plain "word list"
or "VLM field" means *this needs different or additional work to fully
match VidAngel's rule*, not that the rule is out of scope.

- **Word list** (build now): Profanity, Blasphemy, Childish/Crude Language,
  Racial Slurs, and the single-word parts of Sexual References (`porno`,
  `pornography`, `prostitute`, `slut`, `whore`) are a category → word →
  default-enabled structure in a new deep module (see below), each word
  individually toggleable.
- **VLM field, via perceive-then-classify** (architecture decision — see
  below, not a straightforward "more fields in one call"): every
  SEX/NUDITY/KISSING/IMMODESTY/VIOLENCE/ALCOHOL & DRUGS/VULGAR GESTURES/
  OBJECTIONABLE-DISTURBING-SCARY subcategory becomes something the
  classification step derives from a cached description, not a field the
  VLM answers directly in the same forced-choice call as everything else.
  Guidance text for each is drafted from VidAngel's definitions above,
  bound by every rule in [Carrying forward what already
  works](#carrying-forward-what-already-works) — tiered categories are
  primitives-in/code-derives-tier, not a direct tier judgment;
  Violence/Gore get their own "ignore what isn't real" exclusion;
  judgment-call categories are `tentative`. Accuracy is still a
  real-content tuning exercise afterward, same as every field shipped to
  date — this decomposition changes *how cheaply* that tuning can happen,
  not whether it's needed.
- **Word + context** (build now): "hell" (profane vs. literal afterlife
  discussion) and "God"/"Jesus"/etc. (derogatory vs. reverent) get a
  lightweight context check, not just an isolated-word match — the word
  lookup already runs against the full subtitle/whisper cue text, not a
  pre-tokenized word in isolation, so it can check the *same cue* for a
  small disqualifying-context word list (e.g. "hell" is exempted when its
  cue also contains "heaven"/"afterlife"/similar) before counting it as
  profane. This is a pattern heuristic, not true language understanding —
  it catches VidAngel's own stated examples, not every possible phrasing —
  and that limitation is documented on the field, not hidden. "Shut up"
  (mean vs. plain expression) has no comparable nearby-keyword signal to
  pattern-match against, so it ships as a plain always-flagged word for
  now with the nuance noted in its own description; tightening it further
  is a smaller, separate follow-up once the rest of this ships, not a
  blocker to shipping the category.
- **Rating-gated** (decision made: use Jellyfin's own `OfficialRating`).
  Several rules vary by the film's own rating — `crap`/`friggin'`/
  `frickin'` (G/PG only), "hot"/"sexy" commentary (not filtered in
  R/TV-MA), alcohol/tobacco consumption (not filtered at all in R/TV-MA),
  drug-consumption thresholds by tier. Jellyfin already stores this per
  item (`OfficialRating`) and already uses it for its own parental
  controls, so the plugin passes it along as job context (alongside path,
  same as today) rather than this project inventing a second rating
  system. Worker-side, both the word lookup and `classify()` accept an
  optional rating, normalized into VidAngel's own tiers
  (`G/PG` · `PG-13/TV-14` · `R/TV-MA`), and rating-gated rules branch on
  it. A file with no rating in its Jellyfin metadata (common for a
  personal rip) defaults to the **strictest tier's rules (G/PG)** — the
  safer failure mode is filtering something an unrated-but-actually-R film
  didn't need filtered, not silently under-filtering because the rating
  was blank.
- **Subjective/volume-based** (VLM field, explicitly softer): immodesty in
  period pieces or films where it's "prevalent throughout" is VidAngel's
  own judgment call, not a crisp per-instance rule. Ships as a normal VLM
  field with guidance text that says so, rather than pretending a precise
  rule exists where VidAngel itself doesn't have one.
- **Tag, not a field** (build now, cheap): Animal Violence is not a new
  `OBSERVATIONS` field — it's the existing Violence fields' finding with a
  descriptive tag noting the subject is an animal, matching how VidAngel
  itself files it under Violence rather than as its own category.
- **Structural, not VLM, but built now**: Credits (opening/closing/
  recap/ads) is a "where does this segment start and end" problem, not a
  per-frame content judgment, so it does not go on the VLM's per-frame
  call — it's a new small detector, `worker/engines/credits.py`, that
  reuses the shot list `shots.py` already computes for the visual pass
  (no new dependency). Heuristic: within a configurable window at the
  start/end of the runtime (default first/last 10%), a stretch of
  unusually long, low-cut-frequency shots is a candidate credits region.
  It produces ordinary `Segment` entries with category `credits`, reviewed
  and approved exactly like any other finding — not auto-applied — so a
  heuristic miss costs a rejected suggestion, not a bad cut.

**Word taxonomy module** (new, replacing the bulk of
`worker/engines/profanity.py`'s data). A deep module: a plain Python dict
structure, `{category: {word: default_enabled}}`, in the same style as
today's `STRONG_WORDS`/`MILD_WORDS`/`BLASPHEMY_WORDS`, reorganized per the
taxonomy above (slurs split out; sexual-reference words moved out of
Profanity to match VidAngel's own placement). Word-family prefix matching
(`fuckin'`, `screwing`, etc.) stays, gated by whichever category/word owns
that root. The module's entire public surface is one lookup: given a
normalized word, the enabled-word set for the current settings, and the
admin's extra words, is this word flagged? `subtitle_engine.py`,
`whisper_engine.py`, and `resolve_flags()` never see a category name, only
the lookup's boolean answer. This is the module Testing Decisions targets
directly.

**Taxonomy is administrator-extensible, not fixed to the categories above.**
The settings-store word structure is keyed by category name generically —
a *built-in* category (the six named above) has a code-side default word
list the settings store overrides/extends per word; anything else is a
*custom* category the administrator created from the Filters tab, with no
code default at all — every word in it came from the admin. Both shapes
share one lookup and one UI rendering path, so "add a whole new category"
and "toggle one word in an existing one" are the same mechanism at
different starting points, not two features. From the Filters tab: **New
category** (name + first word) creates a custom category; **Add word**
works inside any category, built-in or custom; a custom category (and any
admin-added word inside a built-in one) can be deleted outright. A
built-in category's own shipped words can only be toggled off, never
deleted — removing the toggle-off/toggle-on distinction for built-in words
would make "this word doesn't exist" and "this word is off" the same
state, which is confusing to land on by accident.

**`worker/policy.py` extension.** `OBSERVATIONS` grows to cover every VLM
field tagged above. `DEFAULT_FIELD_GUIDANCE` grows to match. `Policy` (the
dataclass `classify()` reads) grows with whatever new opt-in/opt-out flags
the new categories need, following the existing `flag_male_shirtless`/
`flag_underwear`/`flag_any_kissing` pattern, plus the new `rating` input
the rating-gated rules read. `classify()`'s signature and "most to least
serious" ordering shape are unchanged — new categories are new branches,
not a restructure. `observe_json_footer()` keeps generating the fixed JSON
schema from `OBSERVATIONS` automatically, so the "never admin-editable"
contract holds for the full field set with no extra code.

**Rating passthrough.** The plugin already resolves the Jellyfin item to
queue a job; it adds the item's `OfficialRating` to that same call. The
worker threads it through as plain job `options` (like `language` or
`model` already are) down to the word lookup and `Policy`/`classify()` —
no new endpoint, no new settings-store field, since a rating belongs to
the *film*, not to worker configuration.

**`worker/settings.py` schema growth.** `WorkerSettings` gains a per-word
override structure (category → word → enabled) alongside the existing
`vlmGuidance` per-field override dict, following the same "empty/absent
means use the built-in default" convention already established for
`vlmGuidance`. The existing `profanityIncludeMild`, `profanityIncludeBlasphemy`,
and `profanityExtraWords` fields are migrated, not dropped — a worker
reading an old settings file with those three fields set and no new
per-word structure present derives the equivalent per-word state from them
once, so an existing install's filtering behavior does not silently reset
the first time it runs this build. New visual-category toggles join the
existing trio in the same model. Because the plugin passes worker settings
through as an opaque `JsonElement`, none of this Python-side schema growth
requires any C# change.

**Plugin UI.** The current "Advanced" tab splits into two: **Filters**
(new — the full taxonomy above) and **Advanced** (existing content,
unchanged: VLM host pool, per-field guidance overrides, recovery helper
toggle). Filters is organized by top-level category card, each with its
subcategories/word lists collapsed by default given the taxonomy's size,
with a per-category "reset to default" action alongside the existing
per-field "Reset all to defaults" pattern already shipped for VLM guidance.
A search box filters visible words/categories at once. Each category card
also carries **New category**/**Add word**/delete controls per the
taxonomy-extensibility decision above. Rendering follows the existing
`buildGuidanceFields()` pattern (build DOM once from a data-driven list,
fill from the loaded settings view, read back into the save payload)
rather than inventing a new UI pattern. Credits findings appear in the
existing review UI exactly like any other engine's findings — a suggested
segment, approve or reject — so no new review surface is needed for it.

**Perceive-then-classify — the core architecture for both detection
surfaces, not just an optimization.** Superseding the earlier draft's
"add more fields to the same forced-choice call": asking one small (4B)
vision model to simultaneously *perceive* a frame and *judge* it against
~25 independent criteria in one pass risks the well-documented multi-task
dilution problem — attention spreads thinner as the question count grows,
and a small model has limited capacity to hold many independent
judgments from one image. The fix is decomposing perception from
classification into two stages, which is not a new pattern for this
project — `worker/engines/whisper_engine.py` **already works exactly this
way**: it caches the full transcript as a `.whisper.json` sidecar
specifically "so wordlist changes only re-match, never re-transcribe."
This extends that same proven shape to the visual side, and adds a
second pass to the audio side that doesn't exist yet:

1. **Visual perception** (VLM, per sampled frame, replaces today's
   multi-field JSON call): a single, well-defined task — produce a rich,
   **structured, guided description**, not a free-form caption. Guided
   because a generic "describe this image" prompt talks about the gist
   and skips exactly the details classification needs (the bikini, the
   tattoo needle, the bottle in the background) — the prompt explicitly
   directs coverage of clothing/coverage, visible injury or blood, visible
   substances/paraphernalia, gestures, and notable objects/signage, in
   addition to whatever else is visible. Cached as a new sidecar
   (`.vlm-description.json`, alongside the existing `.shots.json`/
   `.vlm-progress.json`), keyed by model + description-prompt digest —
   the same `_prompt_digest` invalidation mechanism already protecting
   `.vlm-progress.json` today. Deliberately broader than what's classified
   at ship time: a category added later can potentially be classified
   from an *already-cached* description with no GPU re-run, but only if
   the description prompt was already broad enough to mention it — this
   is the one place where being generous now pays off later, so guidance
   coverage should exceed today's taxonomy on purpose.
2. **Visual classification** (new, text-only, no image tokens — cheap):
   a separate pass reads the cached description and derives every
   `OBSERVATIONS` field/tier via `worker/policy.py`'s `classify()`,
   the same "primitives in, code decides" pattern already established.
   Because it's text-only, it can run against Ollama at a fraction of the
   vision call's cost, be re-run freely as guidance text or the taxonomy
   itself changes, and even be run redundantly (e.g. voting) far more
   cheaply than a second vision pass ever could.
3. **Audio semantic pass** (new): today, `is_profane()` only ever sees
   individual words from the already-cached `.whisper.json` transcript.
   A second, equally cheap text-only pass over that *same* cached
   transcript (no re-transcription) catches what pure word-matching
   structurally cannot: phrase-level Sexual References (affair talk, STD
   references), the context-word exemptions from the taxonomy above
   (hell/heaven, god/reverent usage), and — notably — VidAngel's own
   **Implied Sex** category ("sex happens off-screen"), which the visual
   pass has nothing to see and would always miss, but dialogue often
   telegraphs.

**Given a known real deployment is memory-constrained** (8GB unified
memory, also running Jellyfin itself), this shape is also the better cost
profile of the options considered: one vision call per frame (perception
only, roughly the cost of today's call) plus cheap text-only classification
passes, versus either a single more-diluted mega-call or two full
vision passes (which would double the expensive image-encoding cost). The
rollout should still include a rough latency/token measurement of the
perception call alone, and a real-content check that the guided
description actually surfaces the details each category's classifier
needs — that's the one assumption in this design that isn't free to
verify, and it's worth confirming before committing to it as the shape
for every future category too.

## Testing Decisions

Good tests here check external behavior — given this input and this
settings state, is the output right — never internal structure, matching
how `worker/engines/profanity.py`'s `is_profane()` and `worker/policy.py`'s
`classify()` are already tested today (see this session's
`tests/test_policy.py`, `tests/test_profanity_settings.py`).

- **Word taxonomy module**: exhaustive coverage of every category's default
  state, individual per-word override (on and off), extra-words addition,
  and the prefix/word-family matching behavior — pure functions, no I/O,
  cheap to test completely.
- **`worker/policy.py`'s extended `classify()`**: one test per new category
  proving the right category comes back for its observation combination,
  plus the existing "most to least serious" ordering tests extended to
  confirm new categories (especially Gore/Sexual Assault, which should
  outrank milder categories) slot into that ordering sensibly.
- **Settings migration**: a test that constructs the *old* three-flag shape
  on disk and asserts the worker derives the correct per-word state from it
  on first load, plus a test that a *fresh* install (no settings file at
  all) gets sensible taxonomy defaults with no migration step involved.
- **Context-word exemptions** ("hell"/heaven, "god"/reverent-phrase): table
  tests over real VidAngel-style sentence pairs — the profane usage still
  flags, the exempted usage doesn't — same style as the rest of the module,
  still pure functions.
- **Rating-gated rules**: one test per rating-gated rule per tier (e.g.
  `crap` flagged for `PG`, not flagged for `R`), plus the no-rating-present
  case asserting the G/PG (strictest) rules applied.
- **Custom-category CRUD**: create a custom category, add/remove a word,
  confirm a built-in category's shipped word can be toggled but not
  deleted, confirm an admin-added word inside a built-in category can be
  deleted. Same settings-store round-trip style as `tests/test_settings.py`.
- **`worker/engines/credits.py`**: unit tests against a synthetic shot list
  (same `Shot`/`save_shots` fixtures `tests/test_vlm_pool.py` already
  builds) proving the low-cut-frequency window at the start/end is found
  and a normal, evenly-cut middle-of-film stretch is not.
- **Visual classification-from-description** (the new text-only pass):
  given a fixed, synthetic cached description (no VLM call needed), assert
  the right `OBSERVATIONS`/tier comes back — this is the module that lets
  "did we classify correctly" be tested without a GPU or Ollama at all,
  same spirit as stubbing `_grab`/`_ask` today.
  `.vlm-description.json` cache invalidation on a description-prompt
  change: same `_prompt_digest` pattern already covering
  `.vlm-progress.json`, one test confirming a stale cache is discarded
  and a matching one is reused.
- **Audio semantic pass**: fixed transcript fixtures covering the Implied
  Sex / phrase-level Sexual References cases from the taxonomy, asserting
  the pass flags them from cached transcript text with no re-transcription
  call made (mock/spy on the transcription step to prove it's skipped).
- **Not unit-tested, verified manually instead**: whether the guided
  description prompt actually surfaces the details each classifier needs,
  actual VLM/semantic-pass detection accuracy for every new field, and the
  credits heuristic's real hit rate against real films. All need real
  content and a running Ollama instance — the existing
  `tests/test_vlm_pool.py` pattern of stubbing `_grab`/`_ask` proves the
  dispatch/prompt-assembly plumbing works, not that "graphic" vs. "gore"
  is judged correctly or that a real end-credits sequence gets caught,
  which are real-content tuning exercises same as the original eight
  fields already needed.

## Detection Accuracy Testing Plan

The plumbing tests above prove the code runs; they cannot prove detection
is any good. This is the plan for that, and it is not a one-time check —
**every future change to guidance text, the taxonomy, or the detection
backend re-runs this before shipping**, the same way a code change re-runs
the unit tests. A wording tweak that fixes one category's accuracy and
silently breaks another's is exactly the failure mode a fixed, repeatable
corpus catches and an ad hoc spot-check does not.

**The corpus (built, real, checked in as tooling — not as data).**
`eval/build_corpus.py` exists and is verified working: it reads the
*approved* findings out of already-reviewed films' real sidecars (the
closest thing this project has to ground truth — see `worker/review.py`'s
own "nothing acts on a finding until it is approved"), cuts a padded clip
around each one, and concatenates them into one compact video plus a
manifest recording exactly which millisecond range is the true-positive
region and which is clean padding a candidate should *not* flag. Padding
means the corpus scores false positives, not just recall. Current state:
`eval/corpus/v1/`, 37 clips / 5.2 minutes, built from Iron Man 3 (2013)
(34 approved findings — profanity and VLM-flagged `suggestive` content)
and *Thor: The Dark World*'s rendered Clean copy (3 manually-flagged
findings, kept on the copy rather than guess-migrated — see
`review_target()`'s handling of a copy with findings of its own). The
corpus and its source films are gitignored (`movies/`-style — real film
content never gets committed), same as every other real test file in this
project; only the *tooling* that builds it is checked in.

**The real gap: most of the new taxonomy has zero ground truth today.**
The corpus can only contain what's already been approved somewhere, and
nothing has ever detected Violence, Alcohol & Drugs, Immodesty, Vulgar
Gestures, or Objectionable/Disturbing/Scary — so the corpus currently has
*no* labeled examples of any of them. Testing "does a candidate correctly
find graphic violence" needs at least a handful of real, hand-flagged
examples of graphic violence to test against first — this has to be built
deliberately (watch known films, flag genuine examples of each new
category by hand through the existing manual-flag path, same mechanism
already used for the Thor case) before those specific categories can be
evaluated at all, not assumed to work because the mechanism is generic.
Building out this ground truth, category by category, is itself part of
this plan, not a precondition being waved past.

**The comparison harness (not yet built — next piece).**
`eval/run_comparison.py`: given the manifest and a candidate detector
configuration, run detection against the corpus and report, per category:
recall (true positives caught), false-positive rate (padding wrongly
flagged), and latency per frame. The detector side is a small, pluggable
interface — "given this frame/clip, return observations, and how long it
took" — deliberately backend-agnostic so any candidate can be dropped in
without changing the harness:

- **Baseline**: today's Ollama + `qwen3-vl:4b-instruct`, both the current
  single-call shape and the perceive-then-classify shape, so the
  architecture change itself is measured against the corpus, not just
  assumed to be an improvement.
- **FastVLM** (Apple, CVPR 2025): the strongest published speed claim
  found (85x faster time-to-first-token than a similarly-sized model), but
  ships as a Python script/iOS export with no HTTP server, no Ollama
  integration, and no MLX integration — using it means building a small
  serving wrapper first. Worth doing specifically because the speed
  headroom is what makes the "several small focused passes per frame"
  approach affordable instead of theoretical.
- **Moondream2** (1.9B, already in Ollama's library): zero integration
  cost — just a model-tag swap — and built for low memory (usable under
  4GB), the most direct near-term option for hardware like an 8GB
  unified-memory Mac.
- **MLX** (Apple's own framework): 15-30% faster and ~10% less memory than
  Ollama at the same quantization on Apple Silicon specifically, with
  workable support for models close to the current family (Qwen2.5-VL). A
  lower-effort win than FastVLM if the gap turns out to matter less than
  the integration cost.

Each candidate is scored on **both axes together, never accuracy alone**:
a faster model that misses more real content is not automatically the
better choice, and the harness's report should make that trade-off visible
rather than picking a single winner unilaterally.

**What "repeatable" means in practice.** The harness must be cheap enough
to actually re-run, not a ceremony reserved for major releases: given the
corpus is fixed and small (minutes, not hours), a full comparison run
should complete in a timeframe that makes it reasonable to run after every
meaningful guidance-text or taxonomy change, not just before a release.
That's the whole point of building the corpus once instead of re-analyzing
full films per experiment (see [Carrying forward what already
works](#carrying-forward-what-already-works) and the perceive-then-classify
architecture above) — it only pays off if it's actually used every time,
not occasionally.

## Out of Scope

Nothing in the taxonomy itself is out of scope — every category and
mechanism above (including taxonomy-structure editing, rating-aware rules,
and the Credits detector) is committed work for this PRD, not deferred.
What's genuinely outside this document's boundary:

- **`worker/supervisor.py` and the recovery-helper restart mechanism** —
  an unrelated, already-shipped system; see the naming note at the top of
  this document. Nothing here touches it.
- **The distributed-workers PRD's "internal supervisor"/"external
  watchdog" concept** — also unrelated, also already flagged above.

## Further Notes

The taxonomy above is transcribed in full from VidAngel's own published
filter guide, with every "etc." in a word list completed to an actual word
where the source gave enough examples to extrapolate the pattern (e.g.
`pr*ck`/`c*nt`/`tw*t`/`sk*nk`/`d*ck`/`c*ck`/`p*ssy` → `prick`/`cunt`/`twat`/
`skank`/`dick`/`cock`/`pussy`). Where completing a list would mean
speculatively inventing sensitive content this document has no source for
(broader slurs beyond what's already in the codebase), it's left as an
explicitly extensible category instead of guessed at.

This project's own hard-won lesson (CLAUDE.md: negative instructions don't
reliably work on the VLM, confidence is uncalibrated) is still the
higher-priority constraint whenever VidAngel's phrasing and this project's
own prior tuning experience pull in different directions for how a
*guidance field* should be worded — the taxonomy (what exists, what it's
called, what it covers) is taken from VidAngel as specified above; the
prompt engineering to make the VLM actually detect it reliably is this
project's own work to get right, same as it was for the original eight
fields.
