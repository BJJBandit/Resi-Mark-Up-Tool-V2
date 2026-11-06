# Office practice

These are the office's standard choices. Claude follows them in every step unless the
engineer's direction for a job says otherwise, and still sizes every member for its span,
load and site from the standards (the sizes below show the form, not a size to copy).
Edit this file to change them - one rule per bullet.

## Lintels and wall framing
- Lintels are single F17 hardwood or LVL members, normally from the office range L1 90x35, L2 120x35, L3 140x45, L4 190x45 F17/LVL, sized to the AS 1684.2 lintel span tables for the opening width and the load the wall actually carries. Do not use doubled MGP members as the default; use a deeper single F17/LVL (or a larger LVL) before doubling. Schedule them as '<DEPTH>x<BREADTH> F17/LVL LINTEL'.
- Truss / rafter direction: trusses and common rafters span the SHORTER direction of the building (or of each roof wing), perpendicular to the ridge, bearing on the two LONG walls; the two short end walls are gable / hip ends. Work it out from the drawings, in this order: (1) the roof plan - the ridge line runs along the building's long direction, and the fall arrows point down the slope, i.e. along the truss span; sheeting ribs drawn on the roof plan also run down the slope (they do NOT show the ridge); (2) the overall plan dimensions - compare the two overall dimensions (e.g. 16330 vs 8870: trusses span the 8870 way); the measured outline given with each plan page says which way is shorter; (3) the sections. Use another direction only where the drawings clearly show it, and say why. State the assumed span direction and the walls the trusses bear on in the notes, and draw it with the T span arrow; the roof load width for each bearing wall is half that span plus the eaves overhang.
- Work out which walls carry roof load before sizing lintels: from the truss / rafter span direction, the walls the trusses or rafters bear on carry roof load (roof load width = half the span each side plus any overhang); gable-end walls, walls parallel to the truss span and internal non-loadbearing walls do not. Size lintels in walls that carry no roof load for the wall / ceiling / sheeting only - usually L1 90x35 or L2 120x35 - and size the others for their roof load width. State the roof load width used for each loadbearing wall in the notes. Girder trusses, point loads and carport / verandah beams bearing on a wall are added to that wall.
- Where one member runs continuously over several openings or carries a carport / verandah along a wall, use a continuous lintel / ledger (e.g. 190x45 F17/LVL CONTINUOUS LINTEL/LEDGER) rather than separate short lintels.
- Jamb studs: min F17 studs to the sides of openings over 1500 wide; 2/90x35 F17 nail-laminated double studs (DS) to the sides of openings over 2000 wide and under girder / concentrated loads. All studs over 2600 high are F17.
- Lintel tie-down: typical tie-down type (a) to the sides of lintels - 30x0.8 G.I. strap with 6/30x2.8 dia nails each end of the strap and M10 bolts to floor; double strap tie-down type (b) and M12 bolt to floor framing for lintels spanning 1800 and over.

## Bracing
- Use a mix of bracing types to AS 1684.2: type h(b) structural ply panels (tag 'h(b) - <length>', e.g. h(b) - 900) on external walls, type d diagonal strap bracing (tag 'd - <length>', e.g. d - 2400, drawn dashed) along full-length internal walls, and type g where a short ply panel is needed (e.g. g - 600).
- Distribute bracing walls through the whole building in both directions, including internal walls, at the spacing AS 1684.2 Clause 8.3.6 allows - not only on the external walls.

## Footings (AS 2870)
- Strip footings: 450 wide x min 450 deep with 4/L11 TM top and bottom, 50 cover (never narrower than 450).
- Internal beams: 300 wide x min 450 deep with 3/L11 TM bottom (made 450 wide where AS 2870 gives 300 - firm rule).
- Bored piers: 450 dia at corners, steps and articulation joints and at 3000 centres under strip footings (closer only where the ground or loads need it - say why), 1/N16 central, lapped into the footing; founded as the geotechnical report requires (to rock / the stated bearing stratum).
- Isolated brick piers under bearers: 230x230 up to 1500 high; 350x350 where higher; each on a 450 dia bored pier; at 3000 centres along each bearer (the bearer is sized to span 3000), plus at bearer ends and joins. Do not add piers closer than the office spacing unless a load needs one - say why.
- Carport / garage slabs: 100 thick reinforced slab with SL82 top, 450 wide x min 450 deep edge beams with 4/L11 TM top and bottom, 300 x 450 internal beams with 3/L11 TM bottom.

## Floor framing
- Timber floor framing is F7 treated pine (TP) unless spans need more: e.g. B1 2/240x45 F7 TP continuous bearer on piers at 3000 centres (or 2/140x45 F17/LVL equivalent), J1 140x45 F7 TP continuous joists at 450 centres.
- Keep member types to a minimum: where the same kind of member (joists, deck joists, bearers, rafters, lintels in one wall) has a shorter span in one area, use the size needed for the longest span everywhere and give it ONE tag (e.g. all deck joists J2, not J2 and J3).
- Joists span wall to wall: draw the representative joist from external wall / footing to external wall / footing, continuous over the bearers in between (not bearer to bearer). Deck joists run from the house ledger to the outer deck bearer.
- Decks are timber - bearers, joists AND ledgers - unless the engineer's direction for the job says otherwise. A steel / proprietary deck system named on the architectural drawings (e.g. a bushfire-rated steel system) is NOT used: draw the deck in timber and list the named system as an engineer check ("ARCHITECTURAL DRAWINGS NAME <SYSTEM> - DECK DESIGNED IN TIMBER, CONFIRM"). Typical: deck bearers 2/190x45 or 2/240x45 F7 TP, deck joists 140x45 F7 TP at 400 centres (block between as needed), deck ledgers 190x45 / 240x45 F7 TP bolted through to the floor framing with M12 bolts / threaded rod at each end and max 600 centres. If the BAL rating may affect the deck material, keep it timber and list it as an engineer check.
- Columns: 89x3.5 SHS (89x5 SHS where heavier), on a 90x190x10 plate with 4/M12 4.6 bolts chemset (min 100 embedment) into a 450 dia bored pier; continuous columns where a column carries through to the roof.

## Site classification
- Use the site class the geotechnical report gives. A layer or band of low-bearing material is dealt with by founding below it (state the founding depth / stratum), not by re-classifying the site as P. Classify P only if the report itself says P (or gives no class and the conditions clearly require it), and say why.
