# Changelog

## 0.9.0 (beta)

First public version.

- Any number of trucks and wash plants. Loaded trucks go where their load gets washed soonest, so plants take turns
- Route planner over the live terrain: avoids steep slopes, trees, rocks, buildings, water, parked machines and the plants; drives out forward or reverses out, backs into the hopper
- Queue at the excavator, one truck at a time per hopper, trucks stop behind each other and go round trucks waiting on their way
- Auto-send when a truck is full
- Menu in the game (F6) with a setup checklist; overlay with route lines, labels, load/tip markers and a status panel with a mini map, all adjustable
- Load spot, fleet and plants remembered per save
- Finds the save that is actually loaded, stops writing at once when the game closes or loads another save, refuses to connect if the game data doesn't look right
- Hotkeys only active while the game is in front
- Works with game version 0.1.3.3 (finds the new truck bed layout by itself; tipping turns off if it can't)
