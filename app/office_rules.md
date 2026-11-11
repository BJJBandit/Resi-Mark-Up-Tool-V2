# Office practice

These are the office's standard choices. Claude follows them in every step unless the
engineer's direction for a job says otherwise, and still sizes every member for its span,
load and site from the standards (the sizes below show the form, not a size to copy).
Edit this file to change them - one rule per bullet.

## Lintels and wall framing
- Lintels are single F17 hardwood or LVL members, normally from the office range L1 90x35, L2 120x35, L3 140x45, L4 190x45 F17/LVL, sized to the AS 1684.2 lintel span tables for the opening width and the load the wall actually carries. Do not use doubled MGP members as the default; use a deeper single F17/LVL (or a larger LVL) before doubling. Schedule them as '<DEPTH>x<BREADTH> F17/LVL LINTEL'.
- Truss / rafter direction: trusses and common rafters span the SHORTER direction of the building (or of each roof wing), perpendicular to the ridge, bearing on the two LONG walls; the two short end walls are gable / hip ends. Work it out from the drawings, in this order: (1) the roof plan - the ridge line runs along the building's long direction, and the fall arrows point down the slope, i.e. along the truss span; sheeting ribs drawn on the roof plan also run down the slope (they do NOT show the ridge); (2) the overall plan dimensions - compare the two overall dimensions (e.g. 16330 vs 8870: trusses span the 8870 way); the measured outline given with each plan page says which way is shorter; (3) the sections. Use another direction only where the drawings clearly show it, and say why. State the assumed span direction and the walls the trusses bear on in the notes, and draw it with the T span arrow; the roof load width for each bearing wall is half that span plus the eaves overhang.
- Work out which walls carry roof load before sizing lintels: from the truss / rafter span direction, the walls the trusses or rafters bear on carry roof load (roof load width = half the span each side plus any overhang); gable-end walls, walls parallel to the truss span and internal non-loadbearing walls do not. Size lintels in walls that carry no roof load for the wall / ceiling / sheeting only - usually L1 90x35 or L2 120x35 - and size the others for their roof load width. State the roof load width used for each loadbearing wall in the notes. Girder trusses, point loads and carport / verandah beams bearing on a wall are added to that wall.
- Lintel defaults (single storey, sheet roof, roof load width up to about 5 m, F17/LVL), as the office sizes them:
  - Walls carrying roof load: openings up to 1200 wide - L1 90x35 (L2 120x35 at the upper end); up to 2400 wide - L3 140x45; larger - L4 190x45 (or LVL by calculation).
  - Gable-end and non-loadbearing walls: L1 90x35 or L2 120x35 whatever the opening width up to 2400.
  - Go larger only for a tile roof, a roof load width over about 5 m, a girder truss / point load or a carport / verandah beam bearing over the opening, or a second storey above - and say why in the notes.
  - Entry doors and other openings up to 1200 wide: L1 90x35.
  - A continuous lintel / ledger along a wall carrying a carport or verandah: L4 190x45 F17/LVL CONTINUOUS LINTEL/LEDGER.
  - Trusses cantilevering over a recessed wall (eaves overhang / truss cantilever): keep the normal lintel size for the wall (do not upsize for the cantilever) and add the callout 'TRUSS MANUFACTURER TO CONFIRM ABILITY FOR TRUSSES TO CANTILEVER.'
- Where one member runs continuously over several openings or carries a carport / verandah along a wall, use a continuous lintel / ledger (e.g. 190x45 F17/LVL CONTINUOUS LINTEL/LEDGER) rather than separate short lintels.
- Jamb studs: min F17 studs to the sides of openings over 1500 wide; 2/90x35 F17 nail-laminated double studs (DS) to the sides of openings over 2000 wide and under girder / concentrated loads. All studs over 2600 high are F17.
- Masonry lintels (brick veneer / masonry outer skin over every window and door): steel, by clear opening span - up to 900: 75x10 FL; 900 to 1500: 100x75x8 UA; 1500 to 3300: 150x100x10 UA; over 3300: by calculation (engineer check). All masonry lintels bear min 150 into the brickwork each end. The app adds the 'MASONRY LINTEL SCHEDULE' box (span / steel size, bearing note) to the wall framing sheet itself - do not repeat the table in the notes; tag masonry lintels on the plan only where one differs from the schedule.
- Lintel tie-down: typical tie-down type (a) to the sides of lintels - 30x0.8 G.I. strap with 6/30x2.8 dia nails each end of the strap and M10 bolts to floor; double strap tie-down type (b) and M12 bolt to floor framing for lintels spanning 1800 and over.

- Alterations with wall work on more than one floor (e.g. an attic dormer and a first-floor window): mark up the floor plan with most of the new wall work, and label every item that is on another floor with that floor in its callout (e.g. 'L4 OVER BED 3 WINDOW A1827 - FIRST FLOOR'), placed where it sits on that floor; add the note 'ITEMS ON OTHER FLOORS ARE LABELLED WITH THEIR FLOOR'.

## Bracing
- Use a mix of bracing types to AS 1684.2: type h(b) structural ply panels (tag 'h(b) - <length>', e.g. h(b) - 900) on external walls, type d diagonal strap bracing (tag 'd - <length>', e.g. d - 2400, drawn dashed) along full-length internal walls, and type g where a short ply panel is needed (e.g. g - 600).
- Distribute bracing walls through the whole building in both directions, including internal walls, at the spacing AS 1684.2 Clause 8.3.6 allows - not only on the external walls.

## Footings (AS 2870)
- Strip footings: 450 wide x min 450 deep with 4/L11 TM top and bottom, 50 cover (never narrower than 450).
- The perimeter footing under a slab - whether called a strip footing or an edge beam (including a stiffened raft edge beam) - is always 450 wide x min 450 deep with 4/L11 TM TOP AND BOTTOM, never 3/L11 TM bottom only.
- Concrete: N25 (25 MPa) for all footings, beams, piers and slabs unless the design needs higher.
- Internal beams: min 400 deep with 3/L11 TM bottom, 50 cover (450 wide - the firm rule makes any 300 wide beam 450).
- Bored piers: 450 dia at corners, steps and articulation joints and at 3000 centres under strip footings / edge beams, each drawn on the plan (dashed circle, P1 tag) - not only described in a callout; where the footing itself is founded on the bearing stratum and piers can be omitted, say so in the notes (closer only where the ground or loads need it - say why), 1/N16 central, lapped into the footing; founded as the geotechnical report requires (to rock / the stated bearing stratum).
- Bearers ending on a brick base wall (strip footing under the perimeter brickwork) bear on an engaged pier: EP1 350x230 brick engaged pier bonded into the base wall, on a P1 bored pier, at every bearer end on the wall (the app adds them). Isolated brick piers are for bearer supports clear of the walls.
- Isolated brick piers under bearers: 230x230 up to 1500 high; 350x350 where higher; each on a 450 dia bored pier; at 3000 centres along each bearer (the bearer is sized to span 3000), plus at bearer ends and joins. Do not add piers closer than the office spacing unless a load needs one - say why.
- External slabs are separate from the building slab: porch, entry, paving, patio, alfresco, deck-level and carport slabs (driveways are ignored) are poured as separate slabs, isolated from the building slab / edge beam (and from any post / column / pier passing through them) with a 10 Abelflex expansion joint and mastic seal - never cast monolithic with the house slab or wrapped in the house edge beam. Porch / paving slabs (not driveways - see below): 100 thick, SL82 mesh top, 30 cover, saw cuts or tooled joints at max 1500 centres, no edge beam unless a load needs one (carport slabs keep their own edge beams, below). Draw each external slab with its own tag (S2, S3 ...) and its own slab line, keep the house edge beam (EB / SF) on the building line, and add a callout at the joint: 'ISOLATE <PORCH / PAVING> SLAB FROM BUILDING SLAB / FOOTING WITH 10 ABELFLEX JOINT AND MASTIC SEAL.' (the app adds the note).
- Driveways are ignored: they are not part of the structural mark-up - no driveway slab, footing, schedule item, callout or note (other than 'driveway by others' where it matters). Strip footings are drawn with both faces dashed.
- Re-entrant corners of a slab: 3/N12 x 2000 long re-entrant bars tied over the slab mesh at every re-entrant (inside) corner, at 45 degrees across the corner, 100 between bars and the first bar 40 from the corner (the app draws them and adds the note).
- Slab mesh: house slabs, paving / porch slabs and carport / garage slabs are 100 thick (unless the design needs more) with SL82 mesh top, 30 cover - not SL72.
- Carport / garage slabs: 100 thick reinforced slab with SL82 top and 450 wide x min 450 deep edge beams with 4/L11 TM top and bottom. No internal beam in a slab up to 8 m long in both directions (a single / double carport or garage); add internal beams (450 wide x min 450 deep, 3/L11 TM bottom) only where a slab dimension is over 8 m or a load needs one - and say why.

## Floor framing
- Timber floor framing is F7 treated pine (TP) unless spans need more: e.g. B1 2/240x45 F7 TP continuous bearer on piers at 3000 centres (or 2/140x45 F17/LVL equivalent), J1 140x45 F7 TP continuous joists at 450 centres.
- Joists under bathrooms, ensuites, WCs, laundries and any other tiled floor areas are at 350 centres (not 450). Keep the same joist size, but give the wet-area joists their own tag with the spacing in the schedule (e.g. 'J3: 140x45 F7 TP CONTINUOUS JOIST AT 350 CTRS UNDER WET / TILED AREAS'), and draw them over those rooms.
- Keep member types to a minimum: where the same kind of member (joists, deck joists, bearers, rafters, lintels in one wall) has a shorter span in one area, use the size needed for the longest span everywhere and give it ONE tag (e.g. all deck joists J2, not J2 and J3).
- Joists span wall to wall: draw the representative joist from external wall / footing to external wall / footing, continuous over the bearers in between (not bearer to bearer). Deck joists run from the house ledger to the outer deck bearer.
- One tag per member across the whole set: beams carrying a deck / verandah / carport roof are VB on every sheet (roof, wall and footing sheets and the calculations alike - never VB on one sheet and RB on another). Every scheduled member has a size - never 'steel lintel by calculation': size it (e.g. an opening widened in existing masonry gets a steel lintel from the masonry lintel schedule or a sized PFC / UB) and show the working on the calculation sheet.
- Tags: ledgers are always tagged LD (LD1, LD2 ...) - deck ledgers, rafter / roof ledgers and ledgers to an existing wall alike - never L, which is for lintels only. Bearers under a deck, ramp or landing (outside the enclosed floor) take the deck bearer tag (e.g. B2 / DB1) along their whole length, including the bearer at the deck's outer edge; the house bearer tag (B1) is only for bearers under the enclosed floor.
- Decks are timber - bearers, joists AND ledgers - unless the engineer's direction for the job says otherwise. A steel / proprietary deck system named on the architectural drawings (e.g. a bushfire-rated steel system) is NOT used: draw the deck in timber and list the named system as an engineer check ("ARCHITECTURAL DRAWINGS NAME <SYSTEM> - DECK DESIGNED IN TIMBER, CONFIRM"). Typical: deck bearers 2/190x45 or 2/240x45 F7 TP, deck joists 140x45 F7 TP at 400 centres (block between as needed), deck ledgers 190x45 / 240x45 F7 TP bolted through to the floor framing with M12 bolts / threaded rod at each end and max 600 centres. If the BAL rating may affect the deck material, keep it timber and list it as an engineer check.
- Columns: 89x3.5 SHS (89x5 SHS where heavier), on a 90x190x10 plate with 4/M12 4.6 bolts chemset (min 100 embedment) into a 450 dia bored pier; continuous columns where a column carries through to the roof.

- Wet areas on an existing suspended slab (Bondek / concrete first floor), especially hob-free / step-free showers falling to a waterstop: the falls need a set-down in the slab or a topping screed. Add an engineer check / note: set-down or screed for the falls, existing slab thickness and capacity for the added screed and tile load, and any cut for a set-down (the app adds the note).

## Site classification
- Use the site class the geotechnical report gives. A layer or band of low-bearing material is dealt with by founding below it (state the founding depth / stratum), not by re-classifying the site as P. Classify P only if the report itself says P (or gives no class and the conditions clearly require it), and say why.

## Member sizes - office stock list
Use these sections first for every member on every mark-up, wherever one works (the smallest that does). Only if none on the list works, use another section and flag it as an engineer check with the reason.
- Timber, F17 / LVL: 90x35, 90x45; 120x35, 2/120x35, 120x45; 140x35, 140x45, 2/140x45; 190x35, 2/190x35, 190x45, 2/190x45; 240x35, 240x45, 2/240x45; 290x35, 2/290x35, 2/290x45.
- Timber, F7 treated pine (TP): 90x45, 2/90x45; 140x35, 140x45, 2/140x45; 190x45, 2/190x35, 2/190x45; 240x45, 2/240x45; 2/290x45; 90 TP post.
- Timber, MGP10: 2/90x35.
- Steel SHS: 75x4, 75x5, 89x3.5, 89x5.
- Steel CHS: 89x5, 89x5.9, 114.3x5.4.
- Steel RHS: 150x100x6, 200x100x6.
- Steel PFC: 180 PFC, 200 PFC, 250 PFC.
- Steel UB: 200UB22, 250UB31.4, 250UB37, 310UB40, 310UB46.2.
- Steel angles and flat bar (masonry lintels): 75x10 FL, 100x75x8 UA, 150x100x10 UA.
- Steel purlins: Lysaght Z/C 15015.
