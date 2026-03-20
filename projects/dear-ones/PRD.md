# Dear Ones — Product Requirements Document

**Status:** PoC in progress (v1 live at /dear-son/ — rename pending)
**Last updated:** 2026-02-26
**Owner:** Mark Schenker

---

## 1. The Concept

A privacy-first app that helps people be intentional about the relationships that matter most — capturing memories, moments, and meaning throughout the year, then transforming them into beautiful, personal letters delivered to the people they love.

**Tagline:** *"Letters from who you were, to who they'll become."*

**The pivot (Feb 26, 2026):** Originally scoped as parent → child only ("Dear Son"). Expanded to any meaningful relationship — children, parents, partners, friends. Same exact mechanic, broader emotional reach.

---

## 2. The Problem

We're bad at being intentional with the people we love. Not because we don't care — but because:
- Life moves fast and we log nothing
- Photo apps capture moments but not *meaning*
- Journaling is generic and produces nothing shareable
- Nobody has time to write heartfelt letters — but they have 30 seconds to log a voice note

The result: relationships that matter deeply leave almost no written trace. Dear Ones fixes that.

---

## 3. Target Users

**Parent → Child**
Capture what it felt like to watch them grow. Letters delivered at milestones or a set age.

**Adult Child → Aging Parent**
Log moments, gratitude, memories. Send a quarterly letter that shows you're paying attention.

**Partner → Partner**
Stay intentional. Log what you love about them, funny moments, things you noticed. A yearly letter that keeps the relationship alive to itself.

**Long-distance friend**
Log the updates, inside jokes, shared history. A letter that says "I still think about you even when we don't talk."

**Primary target:** People who are already sentimental but under-equipped — they *want* to do this, they just don't have the tool.

---

## 4. Core Features

### MVP / PoC (v1 — current build)
- Voice entry (**primary**): Tap, speak, transcribed via Whisper — hero element of the UI
- Text entry: Optional, secondary — for when you can't speak
- Children hardcoded (Rowan + Raven) — relationship model comes in v2
- AI letter generation (Claude, writing in the user's voice)
- Letter vault: draft → edit → seal → send
- Email delivery via AgentMail
- Single-account password auth

### UX Principles
- **Voice-first:** The person page centers on a large, prominent mic button. Recording is the default action.
- **No manual tags:** AI handles categorization and semantic labeling in the background. Users never see or manage tags.
- **Dynamic header:** "Dear Ones" on the home page; "Dear [Name]" on each person's page.
- **Memory feed design — three layers:**
  1. **Recent feed** (daily use): last 10–20 entries, infinite scroll
  2. **Timeline** (browsing): entries grouped by month, collapsed by default
  3. **Search** (finding things): keyword search in v2, semantic search deferred to v4

### v2 — Relationship Model
- **People** (not just children): add any loved one
- **Relationship type**: child, parent, partner, friend, other
- **Delivery frequency per person**: monthly, quarterly, yearly
- Letter prompt adapts to relationship type (tone differs for partner vs parent vs child)
- Multiple recipients in one account
- **Signature name per person** — how the user signs off *to this specific recipient*:
  - To kids: "Dad", "Papa", "Pops", "Daddy"
  - To parents: "Mark", first name, or a childhood nickname
  - To partner: whatever they actually call themselves
  - Stored on the person record, used in letter generation prompt + email signature
  - Default: user's first name (fallback if not set)
  - **UX note:** Don't surface this upfront. Add it as a subtle "edit" option on the person's page — one tap to set, invisible until needed. Zero friction on the main flow.

### v3 — Polish + Privacy
- E2E encryption for entries and photos
- BYOK: user provides own Anthropic/OpenAI key
- Photo attachment on entries
- ~~Tags~~ — AI categorizes entries in background; no manual tagging exposed to user
- Tone calibration: user writes a sample → AI learns their voice
- AI-generated monthly/yearly summaries ("Here's what you captured about Raven in 2026")

### v3 — Premium Tier Features
- **Audio playback:** Store the original audio file alongside the transcript. Each entry shows a play button if audio exists. Lets you hear your own voice — emotionally irreplaceable compared to text alone.
  - Free tier: transcript only
  - Premium: audio stored + playable forever
  - Long-term: recipients could receive the actual voice recording, not just the letter

- **Global voice log (home page):** Record a stream-of-consciousness memory from the *home page* — before selecting any person. AI parses the transcript, identifies which people are mentioned, and automatically routes relevant snippets to each person's memory log.
  - Mirrors how humans actually talk/think — you don't narrate per-person, you just talk
  - Same mechanic as how Mark uses Telegram: unfiltered stream → AI sorts it
  - Unrecognized mentions → "Unsorted" holding bucket for manual assignment
  - Implementation note: harder lift — requires NER (named entity recognition) + routing logic per person in the account

### v4 — Mobile
- React Native (Expo) — iOS first
- App Store submission
- COPPA/GDPR-K compliance

---

## 5. Business Model

| Tier | Price | Details |
|------|-------|---------|
| **Free (BYOK)** | $0 | Bring your own API key. Full features. |
| **Subscription** | ~$4.99/month or $39.99/year | Managed keys, no friction. |
| **Family/Friends** | ~$7.99/month | Multiple people, shared vault with partner |

---

## 6. Architecture

### Current PoC Stack
- FastAPI (Python) + SQLite
- Vanilla HTML/JS + Alpine.js + Tailwind (CDN)
- OpenAI Whisper (voice transcription)
- Anthropic Claude (letter generation)
- AgentMail (email delivery)
- Hosted on VPS at port 8906, path /dear-son/

### Production Stack (future)
- PostgreSQL (swap SQLite when multi-tenant)
- Cloudflare R2 for encrypted photo storage
- Supabase Auth
- React Native mobile frontend
- Fly.io or Railway for production hosting

---

## 7. Phased Build Plan

### ✅ Phase 0 — PoC (done)
- Voice + text logging
- AI letter generation
- Letter vault + email delivery
- Single account (Mark's family — Rowan + Raven)

### 🔲 Phase 1 — Relationship Model (v2)
- [ ] Rename app to "Dear Ones" (UI + service)
- [ ] Rename DB table: `children` → `people`
- [ ] Add `relationship_type` and `delivery_frequency` fields
- [ ] Relationship-aware letter generation prompts
- [ ] UI: Add/edit any person (not just preset children)

### 🔲 Phase 2 — Polish
- [ ] Photo attachments
- [ ] ~~Entry tags~~ — replaced by AI background categorization
- [ ] Tone calibration from writing sample
- [ ] Better letter editor (rich text)
- [ ] AI-generated monthly/yearly summaries

### 🔲 Phase 4 — Search (deferred)
- **v2:** SQLite FTS5 keyword search + date filter (fast to build, covers 90% of use cases)
- **v4:** Semantic search via embeddings (natural language queries: "times Rowan was brave") — needs embeddings API + vector store; deferred until relationship model + mobile are solid

### 🔲 Phase 3 — Mobile
- [ ] React Native (Expo) shell
- [ ] BYOK mode
- [ ] App Store submission

---

## 8. Open Questions

- [ ] What happens to the vault if the user dies? (estate/access planning — critical to the product promise)
- [ ] Co-access: shared vault with a partner?
- [ ] Physical letter printing integration (future)?
- [ ] Does the recipient need an account, or is email delivery enough for v1?
- [x] Domain: **dearones.app** — registered Feb 27, 2026 ✓

## 9. Delivery Channels (Future Roadmap)

**v1:** Email only (via AgentMail). Simple, works for most people.

**Future (when there are real users):** Let recipients choose their delivery method per-person:
- **Email** — default, works for most
- **WhatsApp** — high priority for families in Asia/LatAm/Africa where WhatsApp is the primary communication channel; more involved to integrate (Business API or Twilio)
- **SMS** — fallback for non-smartphone recipients
- **Physical mail** — print + post integration (e.g. Lob API); high emotional value, premium tier candidate

**Note (Feb 2026):** WhatsApp integration explored briefly — more involved than email (Business API approval, template messages, etc.). Deferred until there's user demand to justify the complexity.

### Delivery Branding Rule
Every message — email, WhatsApp, or otherwise — must make clear it comes from Dear Ones, not as a raw personal message. This matters especially for older or less tech-savvy recipients who might not understand AI-assisted writing.

**Implementation:**
- Every delivery includes a subtle header or footer: *"Sent via Dear Ones — a memory letter service"*
- Subject line always includes "Dear Ones" (e.g. "A letter from Mark via Dear Ones")
- The letter itself can be warmly personal (that's the point), but the envelope signals it's a service delivery
- This protects the emotional authenticity of the letter while preventing confusion about its origin

---

## 9. Why This Could Work

- **Timing:** AI ghostwriting is now genuinely good at capturing personal voice
- **Emotional hook:** Among the highest possible — this is about the people you love most
- **Privacy moat:** Most competitors are VC-backed and data-hungry. Dear Ones can own the trustworthy lane.
- **Broad market:** Not just parents — anyone with a meaningful relationship they want to be more intentional about
- **Personal:** Mark is the target user. Already doing a version of this for Rowan and Raven.
- **Low competition:** No one has nailed this combination of intentional logging + AI letter generation + time capsule delivery

---

*"Be intentional about the people that matter."*
