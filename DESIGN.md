---
name: Competition Manager
description: eFootball leagues, cups and rankings under floodlights, on every phone.
colors:
  night-navy: "#07132b"
  deep-navy: "#0b1d3f"
  card-navy: "#0e2148"
  well-navy: "#0a1834"
  touchline: "#1d3563"
  touchline-strong: "#2b4d8a"
  scorebug-gold: "#f5c400"
  scorebug-gold-pressed: "#e0b000"
  scorebug-gold-hover: "#ffd633"
  floodlight-white: "#f5f8ff"
  terrace-text: "#d5e0f7"
  stand-grey: "#9fb2d6"
  link-sky: "#8ab8ff"
  pitch-green: "#13904a"
  win-green: "#5fe39a"
  red-card: "#e5243b"
  danger-red: "#b3152b"
typography:
  display:
    fontFamily: "Barlow Condensed, Arial Narrow, Roboto Condensed, sans-serif"
    fontSize: "40px"
    fontWeight: 800
    lineHeight: 1
    letterSpacing: "0.2px"
  headline:
    fontFamily: "Barlow Condensed, Arial Narrow, Roboto Condensed, sans-serif"
    fontSize: "32px"
    fontWeight: 800
    lineHeight: 1.05
    letterSpacing: "0.2px"
  title:
    fontFamily: "Barlow Condensed, Arial Narrow, Roboto Condensed, sans-serif"
    fontSize: "17px"
    fontWeight: 700
    letterSpacing: "0.8px"
  body:
    fontFamily: "Barlow, system-ui, -apple-system, Segoe UI, Roboto, sans-serif"
    fontSize: "15px"
    fontWeight: 400
    lineHeight: 1.5
  label:
    fontFamily: "Barlow Condensed, Arial Narrow, Roboto Condensed, sans-serif"
    fontSize: "13px"
    fontWeight: 700
    letterSpacing: "0.6px"
  numeral:
    fontFamily: "Barlow Condensed, Arial Narrow, Roboto Condensed, sans-serif"
    fontSize: "44px"
    fontWeight: 800
    lineHeight: 1.05
    letterSpacing: "0.5px"
rounded:
  tag: "3px"
  button-sm: "7px"
  button: "8px"
  field: "9px"
  card: "12px"
spacing:
  xs: "6px"
  sm: "10px"
  md: "14px"
  lg: "16px"
components:
  button-primary:
    backgroundColor: "{colors.scorebug-gold}"
    textColor: "{colors.night-navy}"
    typography: "{typography.title}"
    rounded: "{rounded.button}"
    padding: "11px 16px"
  button-primary-hover:
    backgroundColor: "{colors.scorebug-gold-hover}"
    textColor: "{colors.night-navy}"
  button-ghost:
    backgroundColor: "rgba(255,255,255,.04)"
    textColor: "{colors.floodlight-white}"
    rounded: "{rounded.button}"
    padding: "11px 16px"
  button-danger:
    backgroundColor: "{colors.danger-red}"
    textColor: "#ffffff"
    rounded: "{rounded.button}"
    padding: "11px 16px"
  card:
    backgroundColor: "{colors.card-navy}"
    textColor: "{colors.floodlight-white}"
    rounded: "{rounded.card}"
    padding: "16px"
  field:
    backgroundColor: "{colors.well-navy}"
    textColor: "{colors.floodlight-white}"
    rounded: "{rounded.field}"
    padding: "11px"
  tag:
    backgroundColor: "{colors.touchline}"
    textColor: "{colors.floodlight-white}"
    typography: "{typography.label}"
    rounded: "{rounded.tag}"
    padding: "2px 8px"
  scorebug:
    backgroundColor: "{colors.scorebug-gold}"
    textColor: "{colors.night-navy}"
    typography: "{typography.title}"
    padding: "1px 13px"
---

# Design System: Competition Manager

## Overview

**Creative North Star: "Floodlit Matchday"**

Every screen is a night match under floodlights, seen through a TV broadcast. The ground is deep navy (#07132b) with a faint stadium glow in the top corner and vertical mowing stripes so subtle they read as texture, not pattern. On top of it, the information arrives the way a football broadcast shows it: scores in a gold parallelogram scorebug, headings in tall condensed capitals, short slanted gold tabs marking where each section starts.

The feel is sporty and energetic: slanted edges, gold that lands like a goal graphic, numbers set big and condensed like a stadium screen. The energy comes from shape and type, not from extra color or effects. It is an operating tool first (organizers and teams act on it daily, mostly on phones), so tables and forms stay calm and scannable, and the broadcast flourishes live at the edges: the scorebug, the section tab, the slanted underline under the top bar, the pitch on the Team of the Season.

Same world on both surfaces: the public site (`templates/public/`) and the dashboard (`public/app.html`) share one palette and type pairing. There is one theme, dark; light mode is not part of the system.

**Key Characteristics:**
- Deep navy ground with a soft floodlight glow and faint vertical pitch stripes.
- Gold (#f5c400) as the single accent: scores, primary actions, active states, section tabs.
- Barlow Condensed for everything that speaks (headings, labels, buttons, numbers); Barlow for reading.
- Slanted geometry: skewed tabs (-16° to -24°), parallelogram scorebugs, never decorative curves.
- Inline SVG line icons; no emojis anywhere.

## Colors

A night-match palette: four navies stacked for depth, one floodlight gold, and the green and red of the pitch and the referee's cards for state.

### Primary
- **Scorebug Gold** (#f5c400): the one accent. Scores, primary buttons, the active menu item's edge, section tabs, ranking positions, awards. Pressed state Scorebug Gold Pressed (#e0b000); hover Scorebug Gold Hover (#ffd633). Text on gold is always Night Navy.

### Neutral
- **Night Navy** (#07132b): the page ground, and the ink on gold.
- **Deep Navy** (#0b1d3f): the bottom of the page gradient and secondary panels.
- **Card Navy** (#0e2148): cards and containers.
- **Well Navy** (#0a1834): sunken surfaces: form fields, inner cards, award tiles.
- **Touchline** (#1d3563) and **Touchline Strong** (#2b4d8a): dividers, borders, bracket connectors, tag backgrounds. The public site uses them as translucent lines (rgba(140,170,255,.15) and .28) over the navy.
- **Floodlight White** (#f5f8ff): primary text. **Terrace Text** (#d5e0f7): secondary text. **Stand Grey** (#9fb2d6): notes, captions, metadata.
- **Link Sky** (#8ab8ff): links in running text.

### State
- **Pitch Green** (#13904a): wins, qualification places, success toasts.
- **Win Green** (#5fe39a): positive figures, wildcard (best third) qualifiers.
- **Red Card** (#e5243b): losses, errors, live. **Danger Red** (#b3152b): destructive buttons.

### Named Rules
**The One Floodlight Rule.** Gold is the only accent. It marks what matters (the score, the main action, where you are) and nothing else; a screen where everything is gold has no score.

**The Card Colors Rule.** Green and red mean what they mean on a pitch: win or qualify, lose or danger. Never use them as decoration or as a second accent.

## Typography

**Display Font:** Barlow Condensed (with Arial Narrow, Roboto Condensed)
**Body Font:** Barlow (with system-ui, Segoe UI, Roboto)

**Character:** Tall, narrow broadcast capitals for anything announced (headings, scores, labels, buttons) against a plain, friendly sans for reading. Condensed type fits long team names and big numbers on small phone screens.

### Hierarchy
- **Display** (800, 40px, line-height 1): page titles on the dashboard and public pages; up to clamp-sized hero titles on public pages.
- **Headline** (800, 32px, 1.05): section and card titles, team names on their pages.
- **Title** (700, 17px, 0.8px tracking): card headers with the gold tab, buttons (16px), brand name (21px).
- **Body** (400, 15px, 1.5): running text, form fields, table cells; keep paragraphs near 65ch.
- **Label** (700, 13px, 0.6px tracking, often uppercase): table headers, chips, eyebrows, matchday labels.
- **Numeral** (800, 44px, 1.05): KPI tiles; the matchday board goes to 68px with 4px tracking. Scores in tables use 19–20px condensed gold.

### Named Rules
**The Broadcast Caps Rule.** Anything announced is condensed; anything read is not. Never set paragraphs in Barlow Condensed, and never set a score or a heading in body Barlow.

## Layout

A single centered column on the public site and a left sidebar with a content column on the dashboard; on phones the sidebar becomes a slide-in menu with a backdrop. Cards stack with 14px between them and 16px inside. Grids of tiles use auto-fill columns (around 220–240px minimum) and fall to two or three compact columns on phones (awards go to three columns at ≤640px). Breakpoints in use: 560px, 620px, 640px, 720–760px, 820–900px. Wide tables, brackets and fixture lists scroll inside their own container; the page itself never scrolls sideways. On phones and in landscape at ≤520px tall, the Team of the Season pitch turns sideways (goalkeeper left, forwards right) so the whole XI fits one screen.

## Elevation & Depth

Depth is tonal, not shadowed: Night Navy page, Card Navy cards, Well Navy wells inside them, each step a little lighter or darker. Borders in Touchline blue separate where tone alone isn't enough. The only light source is the floodlight glow in the page background. Where a shadow exists it is soft and low (`0 10px 30px -12px rgba(0,0,0,.6)` with a 4% white inner top edge) and crests get a small drop shadow to lift them off the navy.

### Named Rules
**The Floodlight-Only Rule.** Light comes from the stadium, not from the components: no glowing buttons, no neon outlines, no layered drop shadows on cards.

## Shapes

Firm, slightly rounded rectangles for things you tap (buttons 8px, fields 9px, cards 12px) and sharp, slanted shapes for things that announce: the scorebug and kick-off chips are parallelograms (7px slant via clip-path), section tabs and the top-bar underline are skewed bars (-16° and -24°), tags have 3px corners like a TV caption. Crests are circles with a white rim. Pitch markings are thin white lines at 75% opacity.

## Components

### Buttons
- **Shape:** firm, slightly rounded (8px; 7px for small).
- **Primary:** Scorebug Gold with Night Navy text in Barlow Condensed 700 16px, 11px × 16px padding. Hover lightens to #ffd633.
- **Ghost:** 4% white fill, Touchline Strong border, white text; hover turns the border gold.
- **Danger:** Danger Red with white text; destructive actions ask "Sure?" in place before acting.
- Icons sit left of the label, 6px gap.

### Chips and Tags
- **Style:** Barlow Condensed 700 12.5px, 3px corners, 2px × 8px. Role and status tags take their meaning color (owner tag is gold on navy; live is red; active/finished green).

### Cards / Containers
- **Corner Style:** 12px.
- **Background:** Card Navy; inner wells in Well Navy.
- **Header:** the card title in Barlow Condensed with a small skewed gold tab before it (5px × 16px, -16°).
- **Internal Padding:** 16px; 14px between cards.

### Inputs / Fields
- **Style:** Well Navy fill, Touchline border, 9px corners, 11px padding, 15px text.
- **Focus:** the border turns gold; no glow.
- Password fields have a show/hide eye.

### Navigation
- **Top bar:** near-opaque Night Navy with a skewed gold underline (150px × 3px, -24°) under the brand. Public links are condensed text with line icons; on phones only the icons show.
- **Dashboard sidebar:** grouped sections; the active item gets a gold left edge and a gold-to-transparent wash.

### Scorebug (signature)
Every score in the product: Barlow Condensed 800 20px, 1.5px tracking, Night Navy on Scorebug Gold, parallelogram edges. Kick-off times use the same shape in translucent white.

### Team of the Season pitch (signature)
A striped green pitch with real markings (halfway line, centre circle, boxes, arcs, goals) in thin white lines; player cards with crest, gold position badge (dashed outline when worked out automatically), name and team. Vertical on wide screens, sideways on phones.

## Do's and Don'ts

### Do:
- **Do** keep gold for scores, the main action and "you are here"; one gold focus per area.
- **Do** set scores in the scorebug shape and headings in Barlow Condensed 800.
- **Do** use slanted shapes (skewed tabs, parallelograms) for energy, and keep tappable things as firm rounded rectangles.
- **Do** use inline SVG line icons (2px stroke, round caps) in `currentColor`.
- **Do** design every screen for a phone first; tables and brackets scroll in their own box.

### Don't:
- **Don't** use emojis anywhere; use the SVG icon set.
- **Don't** use real clubs' badges, names or brand marks in logos, art or examples.
- **Don't** add a second accent color or decorative gradients; navy tones and gold carry the brand.
- **Don't** use green or red except for win/qualify and lose/danger.
- **Don't** add glows, neon outlines or heavy shadows; the floodlight in the background is the only light.
- **Don't** pile on effects to make it "sporty"; the energy comes from slant, condensed type and big numbers.
