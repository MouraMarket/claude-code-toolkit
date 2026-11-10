# Pattern catalog

Every pattern this skill removes, with a before and after. Six families, plus the word lists that come first because they are the fastest wins.

A note on how to use these lists: they are guidance, not find-and-replace. A banned word inside a quotation stays. A word the writer or the brand genuinely owns stays. A proper noun stays. The test is whether the word is doing work.

Many of these patterns were first catalogued by Wikipedia's [Signs of AI writing](https://en.wikipedia.org/wiki/Wikipedia:Signs_of_AI_writing) page and the open-source [blader/humanizer](https://github.com/blader/humanizer) skill. The examples below are this catalog's own.

---

## Word and phrase lists

### Banned outright

These almost never survive a good edit:

delve, foster, fostering, leverage (verb), utilize, facilitate, empower, streamline, robust, cutting-edge, paradigm shift, game changer, this is huge, this changes everything, tapestry, realm, beacon, multifaceted, meticulous, intricate, intricacies, paramount, transformative, elevate, embark, supercharge, harness, ever-evolving, testament, vibrant, nestled, breathtaking, must-visit, unparalleled, unforgettable, unmatched, seamless, groundbreaking (figurative), renowned, stunning.

### High-frequency AI vocabulary

Not banned, but each one earns its place or goes. They cluster, so finding one means looking for the rest:

additionally, align with, crucial, emphasizing, enduring, enhance, garner, highlight (verb), interplay, key (adjective), landscape (as abstract noun), pivotal, showcase, underscore (verb), valuable, unwavering, optimize (in puffy contexts).

### Often-empty adverbs

just, literally, honestly, simply, actually, truly, fundamentally, importantly, crucially, inherently, inevitably.

Cut them when they add nothing. Keep them when they carry emphasis, contrast, real uncertainty, or the writer's spoken rhythm.

### Often-empty phrases

it's worth noting, it's important to note, at the end of the day, when it comes to, at its core, in today's world, in the age of, in the world of, the reality is, the truth is, in terms of, with regard to, going forward, in this article, let's dive in, without further ado, picture this, imagine if.

Cut them when they delay the point. Keep an occasional one when it belongs to the writer's recognizable voice and the sentence still earns its place.

### Filler swaps

| Instead of | Write |
|---|---|
| in order to achieve this goal | to achieve this |
| due to the fact that | because |
| at this point in time | now |
| in the event that you need help | if you need help |
| has the ability to process | can process |
| it is important to note that the data shows | the data shows |
| made a decision | decided |
| conducted an analysis of | analyzed |
| is reflective of | reflects |

---

## Family 1: Inflation and puffery

### 1.1 Importance and legacy inflation

**Watch for:** stands as, serves as, is a testament to, is a reminder of, plays a vital/significant/crucial/pivotal/key role, marks a pivotal moment, underscores its importance, reflects broader, symbolizing its enduring, contributing to, setting the stage for, represents a shift, key turning point, evolving landscape, focal point, indelible mark, deeply rooted, solidifies its position.

**Why it happens:** the model inflates significance by tying an arbitrary detail to a broader trend. State the fact and let the reader decide whether it matters.

> **Before:** The neighborhood library added Sunday hours in March, marking a pivotal moment in the community's evolving relationship with public learning and underscoring its enduring commitment to access.

> **After:** The neighborhood library opened on Sundays starting in March, from noon to five.

### 1.2 Promotional and advertisement-like language

**Watch for:** boasts a, vibrant, rich (figurative), profound, enhancing its, showcasing, exemplifies, commitment to, natural beauty, nestled, in the heart of, breathtaking, must-visit, stunning, an oasis of, a sanctuary of, step into a world of, experience the magic of, where memories are made.

> **Before:** Nestled in the heart of the old mill district, the bakery boasts a vibrant atmosphere and a rich tradition of breathtaking sourdough, a must-visit oasis for bread lovers.

> **After:** The bakery is in the old mill district. It bakes sourdough every morning and usually sells out by eleven.

### 1.3 Notability and coverage padding

**Watch for:** independent coverage, local/regional/national media outlets, written by a leading expert, active social media presence, widely regarded as.

Listing outlets is not evidence of anything. One specific claim beats four logos.

> **Before:** The app has been featured across national tech blogs, regional newspapers and popular podcasts, and its founder is widely regarded as a leading voice with a strong social media presence.

> **After:** The app's founder wrote a guide to offline-first sync that several sync libraries now link to from their docs.

### 1.4 Superficial `-ing` analysis

**Watch for:** trailing participial clauses that pretend to explain meaning: highlighting, underscoring, emphasizing, ensuring, reflecting, symbolizing, contributing to, cultivating, fostering, encompassing, showcasing.

Either the clause states a real consequence or it goes. Replace fake depth with the actual mechanism.

> **Before:** The launch adds file search, highlighting the team's commitment to better workflows.

> **After:** The launch adds file search, so users can find old drafts without leaving the editor.

> **Before:** The garden plots are laid out in a spiral, reflecting the group's deep connection to the seasons and symbolizing growth, renewal and shared purpose.

> **After:** The garden plots are laid out in a spiral so every plot gets the same afternoon sun.

### 1.5 Abstraction where a fact belongs

Abstraction is where writing goes to die. Do not smooth a useful detail into generic importance. Protect the specific fact.

> **Before:** The integration significantly improved engineering efficiency.

> **After:** The integration cut deploy time from 40 minutes to 4.

> **Before:** The tool significantly improves review productivity.

> **After:** The tool cut review time from 30 minutes to 8.

---

## Family 2: Structural tics

These are sentence-shape habits. They are the hardest for a writer to see in their own draft and the fastest for a reader to spot.

### 2.1 Binary contrasts and negative parallelism

**Watch for:** "This is not X. It's Y." / "The question isn't X, it's Y." / "It's not just X, it's Y." / "Not only does it..." / "It's not merely..."

State Y directly.

> **Before:** The question isn't the model. It's the eval.

> **After:** The eval matters more than the model.

> **Before:** It's not just a running club; it's a movement. It's not merely exercise, it's a way of life.

> **After:** The running club meets three mornings a week, and most members have stuck with it for years.

### 2.2 Negative listing

**Watch for:** "Not a X. Not a Y. A Z."

Just say Z.

### 2.3 Throat-clearing openers

**Watch for:** Here's the thing, Here's what I mean, Let me be clear, I'll be honest, The uncomfortable truth is, Look.

Cut them and state the point. (Exception: when one of these is genuinely how the writer talks and it appears once, not four times.)

### 2.4 Faux-insight setups

**Watch for:** This is the part most people skip, What most people get wrong, Here's what nobody tells you, The part everyone misses, Most teams don't realize.

These flatter the writer as the lone expert. Cut the setup and let the claim stand alone.

> **Before:** The part everyone misses: distribution is the real moat.

> **After:** Distribution is the moat.

### 2.5 Colon reveals

**Watch for:** a noun phrase, a colon, then a lowercase dramatic reveal. "The detail that makes it work: a separate agent grades it." "The best part: it learns."

Rewrite as a plain sentence. Reserve colons for lists, labels, and quotes.

> **Before:** The detail that makes it work: a separate agent grades it.

> **After:** A separate agent does the grading, which is what makes it work.

Prefer sentence case after a colon unless grammar, a proper noun, a title, or code requires otherwise.

### 2.6 Rhetorical setups

**Watch for:** "What if I told you...", "Think about it:", "Plot twist:", "Sound familiar?", and self-answered "Question? Answer." pairs.

Drop the setup and make the point.

### 2.7 Dramatic fragmentation

**Watch for:** "X. And Y. And Z." / "That's it. That's the whole thing." / stacked one-word sentences for emphasis.

Use complete sentences. One deliberate fragment in a piece is voice; four is a tic.

### 2.8 Rule of three overuse

Three-element lists used as decoration rather than enumeration.

> **Before:** The workshop offers hands-on learning, meaningful connection, and lasting growth. Participants leave with clarity, confidence, and community.

> **After:** The workshop is two hours of building a small project, with time to compare notes at the end.

### 2.9 False ranges

"From X to Y" where X and Y are not endpoints of any real scale.

> **Before:** The newsletter covers everything from the smallest kitchen tip to the grandest culinary philosophy, from humble weeknight dinners to the art of the feast.

> **After:** The newsletter has one weeknight recipe and one kitchen tip each issue.

### 2.10 Robotic rhythm

Repeated sentence shapes, identically structured paragraphs, stacked punchy fragments, every paragraph the same length. Vary the shape only where it helps the point, but do vary it.

### 2.11 Formulaic "challenges and future prospects" sections

**Watch for:** "Despite its... faces several challenges," "Despite these challenges," and headings like "Challenges and Legacy" or "Future Outlook."

Replace the section with specifics or delete it.

> **Before:** Despite its growing popularity, the community pool faces challenges typical of public facilities, including aging equipment and staffing shortages. Despite these challenges, the pool continues to thrive as a cherished local institution.

> **After:** The pool's heater failed twice last winter, and it closed on Mondays from June because it could not hire a third lifeguard.

---

## Family 3: Vagueness and evasion

### 3.1 Weasel attribution

**Watch for:** Experts agree, Industry reports suggest, Observers have noted, Many argue, Some critics argue, Studies show, Research indicates, widely regarded as, several sources.

Name the source or cut the claim. When the draft has no source, ask the user rather than inventing one.

> **Before:** Experts agree that standing desks offer significant benefits, and studies show they can transform workplace wellbeing.

> **After:** [Name the study or expert the claim comes from, or cut it. If the draft has no source, ask the user for one.]

### 3.2 Excessive hedging

Stacked qualifiers that leave no claim standing.

> **Before:** It might perhaps be possible that the new schedule could, in some cases, potentially reduce wait times somewhat.

> **After:** The new schedule may reduce wait times.

Keep single hedges that carry real uncertainty. Cut the stacking, not the honesty.

### 3.3 Knowledge-cutoff and availability disclaimers

**Watch for:** as of my last update, up to my training data, while specific details are limited, based on available information, publicly available sources suggest.

These are chatbot self-reports left in the text. Either supply the fact or say plainly that it is unknown.

> **Before:** While specific details about when the trail was built are limited based on available information, it appears to date from sometime in the mid-twentieth century.

> **After:** The trail's build date is unknown. [Ask the user whether they have one; never guess a decade.]

---

## Family 4: Weak construction

### 4.1 Copula avoidance and fake-strong verbs

**Watch for:** serves as, stands as, marks, represents, functions as, boasts, features, offers.

Prefer "is" and "has" when they are clearer. Better still, replace the whole construction with what the thing actually does.

> **Before:** The back room serves as the club's meeting space. It features two long tables and boasts seating for thirty.

> **After:** The club meets in the back room. It has two long tables and seats thirty.

> **Before:** The app serves as a centralized hub for sponsor management.

> **After:** The app tracks sponsors, drafts, due dates, and approvals in one place.

### 4.2 Weak verb phrases

Let the verb do the work: "made a decision" becomes "decided," "has the ability to" becomes "can," "provides support for" becomes "supports," "is in agreement with" becomes "agrees."

### 4.3 Passive voice and inanimate actors

Use active voice with human subjects where possible, and never let inanimate things perform human verbs.

> **Before:** The decision emerged from the discussion.

> **After:** The team decided on Tuesday.

### 4.4 Synonym cycling

Repetition penalties push models to rotate terms. If the clear word is right, repeat it.

> **Before:** The volunteer sorts the donations. The helper logs each item. The participant stacks the boxes.

> **After:** The volunteer sorts the donations, logs each item and stacks the boxes.

> **Before:** The agent reviews the draft. The assistant scores the piece. The tool suggests fixes.

> **After:** The agent reviews the draft, scores it, and suggests fixes.

### 4.5 Filler and throat-clearing at sentence level

See the filler swaps table above. Every sentence earns its place.

---

## Family 5: Formatting tells

Format should follow the content, not decorate it.

### 5.1 Em dashes

**Hard ban. Zero em dashes in any output, of any length, without exception.**

This is the single most reliable tell, and the rule is absolute rather than a preference so there is nothing to weigh. Every em dash gets replaced with a comma, a period, a colon, a semicolon, or parentheses, whichever the sentence actually wants. Do not argue that a particular one "clearly beats" the alternatives. There is always an alternative.

This applies to en dashes used as sentence punctuation too. En dashes in genuine numeric ranges (2019–2024) are fine.

The only place an em dash survives is inside quoted material, where changing it would misquote the source.

> **Before:** The move went smoothly—mostly—until the last box—the heavy one—jammed in the stairwell.

> **After:** The move went smoothly, mostly, until the last box (the heavy one) jammed in the stairwell.

### 5.2 Boldface overuse

Bold sprinkled mid-sentence for emphasis, or applied mechanically to every term.

> **Before:** The plan covers **budgeting**, **meal prep**, and a weekly **review session** to keep the **household goals** on track.

> **After:** The plan covers budgeting, meal prep, and a weekly review to keep household goals on track.

### 5.3 Inline-header vertical lists

Bulleted lists where every item is a bolded label, a colon, then a sentence restating the label. Often two sentences of prose read better.

> **Before:**
> - **Speed:** The site is now faster thanks to image compression.
> - **Navigation:** Navigation has been simplified with a new menu.
> - **Accessibility:** Accessibility has been improved with better contrast.

> **After:** The site loads faster now that images are compressed, the menu is simpler, and the text has better contrast.

### 5.4 Bullets where prose belongs

A two-item list, or a list whose items are full sentences that flow, is usually prose. Also cut headers over sections shorter than three sentences.

### 5.5 Title case in headings

> **Before:** ## Planning Your Spring Garden Beds

> **After:** ## Planning your spring garden beds

### 5.6 Emoji as decoration

Emoji in headings, or leading every bullet.

> **Before:**
> 📅 **Date:** Saturday the 14th
> 📍 **Place:** The east picnic shelter
> 🍔 **Food:** Bring a dish to share

> **After:** The picnic is Saturday the 14th at the east shelter. Bring a dish to share.

Expressive emoji inside a sentence can be fine when it matches the voice. Emoji as bullet furniture is not.

### 5.7 Curly quotation marks

Replace curly quotes and apostrophes with straight ones: `"` and `'`. **Except letters that only look like punctuation.** The Hawaiian ʻokina (as in Hawaiʻi) is a letter (U+02BB), not an apostrophe: never straighten it, and leave diacritics such as the kahakō (ā, ē, ī, ō, ū) alone. If a draft types the ʻokina as a curly quote (‘), correct it to ʻ.

> **Before:** She called it “a fair deal” and moved on.

> **After:** She called it "a fair deal" and moved on.

---

## Family 6: Openings and endings

### 6.1 Chatbot correspondence artifacts

**Watch for:** Of course!, Certainly!, Great question!, You're absolutely right, I hope this helps, Would you like me to, let me know if, Here is a...

Conversation wrapper pasted in as content. Delete it.

> **Before:** Certainly! Here is a summary of the meeting. I hope this helps, and let me know if you'd like me to expand on anything.

> **After:** The meeting moved the launch to May and gave the pricing page to the design lead.

### 6.2 Sycophantic tone

> **Before:** What a fantastic idea! You're absolutely right to think about this, and that's a really insightful point about the timeline.

> **After:** The timeline you raised is the main risk.

### 6.3 Generic positive conclusions

**Watch for:** the future looks bright, exciting times lie ahead, a major step in the right direction, continues to thrive, on a journey toward excellence.

> **Before:** With these changes, the future looks bright for the choir, and exciting times lie ahead as it continues on its journey.

> **After:** The choir will add a second concert in December.

### 6.4 Summary-recap endings

**Watch for:** In conclusion, Ultimately, Overall, To summarize, or a closing paragraph that restates the piece.

The reader was just there. End on the last concrete point, the takeaway, or the next action.

### 6.5 Fake-profound kickers

The final "deep" line that turns the point into an aphorism, a cute metaphor, or a mic drop. "The future isn't coming. It's already here."

**Delete it. Do not rewrite it into a better metaphor, and do not preserve the rhythm.** End on the clearest concrete sentence already in the draft. If the ending genuinely needs closure, add a plain takeaway or a next action.

This is the pattern most likely to survive an edit, because it feels like the best line in the piece. It usually is not.

---

## Worked example

**Before:**

> Absolutely! Here's an update for the neighborhood association. I hope you find it helpful!
>
> The new community garden stands as a vibrant testament to the power of collective action, marking a pivotal moment in our neighborhood's evolving story. Nestled between the school and the bus depot, this groundbreaking space—born from countless conversations—is reshaping how neighbors connect, collaborate, and grow together.
>
> At its core, the garden is about more than vegetables; it's about fostering belonging, cultivating resilience, and nurturing hope. The garden serves as a gathering place. The plots function as classrooms. The compost stands as a lesson in renewal.
>
> Many residents have noted that interest has grown from a handful of early volunteers to families across every street, from first-time planters to seasoned growers. Additionally, the shared tool shed showcases how small investments can empower big change, highlighting the intricate interplay between community and the land.
>
> - 🌱 **Plots:** Plots have been allocated to interested households.
> - 💧 **Water:** Watering has been streamlined with a new schedule.
> - 🛠️ **Tools:** The tool shed is now available for shared use.
>
> While specific details about next year's funding are limited, it could potentially be argued that the garden might continue in some form. Despite the challenges typical of volunteer projects, the garden continues to thrive.
>
> In conclusion, the future looks bright. Exciting times lie ahead. The garden isn't just growing food — it's growing us.

**Draft rewrite (patterns stripped):**

> The community garden between the school and the bus depot is open.
>
> Plots have gone to the households that asked for them, there is a watering schedule, and the tool shed is open to everyone. [writer's own example goes here]
>
> Funding for next year is not settled. Volunteers have kept it going so far.

**Two-question audit, question one: "What makes the draft below so obviously AI generated?"**

- The rhythm is still too tidy. Three short, evenly paced paragraphs.
- "Funding for next year is not settled. Volunteers have kept it going so far." is a neat two-beat close, a soft kicker.
- The opening line is a flat announcement, not a neighbor talking.
- No stated position. It reports and commits to nothing.

**Final rewrite:**

> The community garden between the school and the bus depot is open. I'm still a little amazed we pulled it off. [writer's own example goes here]
>
> Plots went to every household that asked, there's a watering schedule, and the tools in the shed are for everyone.
>
> Next year's funding isn't settled, and that worries me more than I'd like. [writer's next step goes here]

**What changed:**

- Removed chatbot artifacts and the cheerful opener
- Removed importance inflation: testament, pivotal moment, evolving story
- Removed promotional language: vibrant, nestled, groundbreaking
- Removed weasel attribution: "many residents have noted"
- Removed trailing `-ing` analysis: highlighting, fostering, cultivating
- Removed binary contrast: "more than vegetables; it's about..."
- Removed rule-of-three and synonym cycling: gathering place / classrooms / lesson
- Removed the false range: "from X to Y, from A to B"
- Removed em dashes, emoji bullets, bolded inline headers
- Replaced copula avoidance (serves as, function as, stands as) with plain verbs
- Removed the formulaic challenges line and the availability disclaimer
- Removed stacked hedging and the generic positive conclusion
- Deleted the fake-profound kicker rather than rewriting it
- Added stated feeling (amazement, the funding worry) and varied the rhythm, without adding any new factual claim, and left placeholders where the draft had no example and no next step
