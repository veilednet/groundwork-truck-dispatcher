TRUCK DISPATCHER 0.9.0 (beta) - for Groundwork Simulator
========================================================

Your dump trucks haul by themselves while you dig. Load a truck with the excavator and it drives to the
wash plant with the shortest wait, backs into the hopper, tips, and comes back to wait in line behind
the truck you're loading. Use as many trucks and wash plants as you like.

Single player. Fan-made, not connected to the game's developers. Made for game version 0.1.3.3 (Early Access, November 2026).


START
  1. Run "Truck Dispatcher.exe" (or run.bat if you use the Python version). Before or after the game,
     it doesn't matter: it connects by itself once your save is loaded.
  2. Play in borderless or windowed mode (the game's default). The overlay can't draw over exclusive
     fullscreen.
  3. In the game press F6. The menu's GET STARTED box tells you what's left to do:
       - tick the trucks the dispatcher should drive
       - tick the wash plants it may use
       - park a truck under the excavator's bucket, load it and press F7
     That's it. From then on: load, F7, load, F7... With auto-send on (default) you don't even need F7:
     a truck leaves by itself when it's 95 % full.

HOTKEYS (only while the game is in front; change them in the menu, Settings tab)
  F6   menu over the game (Esc or F6 closes it)
  F7   send the loaded truck at the excavator; idle trucks join the cycle
  F8   put the load spot where the excavator's bucket points
  F9   pause / resume (trucks brake and wait)
  F10  start / stop recording a road

HOW IT DRIVES
  - Trucks plan their own routes over the live terrain: they avoid steep slopes, trees, rocks, buildings,
    water, parked machines and the plants, slow down in curves and on slopes, and keep right.
  - Loaded trucks pick the plant where their load gets washed soonest, so with two plants they take turns.
    One truck at a time backs into each hopper. If a hopper is full, the truck waits for it to wash down.
  - Empty trucks wait in line before the excavator's swing. The next one moves under the bucket as soon
    as the loaded one has pulled away.
  - The load spot follows the excavator when you move along the bench.
  - Get into a truck the dispatcher is driving (or drive one away from the load spot) and it lets go.
    Press F7 or Join in the menu to give it back.

OPTIONAL
  - Roads: press F10, drive a road in any machine, press F10 again. Trucks prefer it, in the direction
    you drove it (make it two-way in the menu, Roads & log tab).
  - Teach tip spot: if a truck model stops short of the hopper or too far in, back one up to the hopper
    yourself and press "Teach tip spot". It's remembered per truck model.
  - Overlay tab: route lines, labels over the trucks, a status panel with a mini map. Everything can be
    switched off, resized, recoloured and made more or less transparent. With the menu open you can drag
    the status panel anywhere and drag its corner to resize it.

PROBLEMS
  "Not connected - the game's terrain doesn't match any autosave yet"
      You just started or loaded a game. The tool reads your autosave to find things in memory, so it
      waits for the game's next autosave (a few minutes) and then connects by itself.
  "Wash plant not found" / "Truck beds not found"
      Something new since the last autosave. Wait for the next autosave and press Reconnect.
  Nothing is drawn over the game
      Use borderless or windowed mode. Labels freezing for a few seconds after you switch view is normal:
      it is finding the new camera.
  A hotkey does nothing
      Another program may use the key. Pick another one in the menu, Settings tab.
  Something else
      Open an issue on GitHub and attach dispatcher.log (menu, Settings tab, Open log).
  Windows SmartScreen / antivirus warning
      The exe isn't signed, and it reads and writes the game's memory, which is what cheat tools do too.
      The full source code is on GitHub if you want to check it or run the Python version instead.

IS IT SAFE?
  It never changes game files or save files. While it runs it writes into the game's memory: the
  positions of the trucks it drives, their tipper and cargo, and the wash plant hopper. The game's
  autosave then stores the result like normal play (trucks where they stopped, dirt in the hopper).
  If the game updates and the data no longer looks right, the tool refuses to connect instead of
  writing anything. It's a beta: keep a backup of your save if it matters to you.

  How it works: the game only runs driving physics and tipping for the machine you sit in, so the tool
  moves idle trucks along their planned path itself (steering and wheels animated) and tips by moving
  the bed's dirt into the hopper with the game's own amounts. The plant then washes it as normal.
  Limits: trucks pass through things the planner doesn't know about (dropped logs, small rocks, you on
  foot), and a truck that blocks another one for a long time is driven through.

FILES (next to the exe)
  config.json     settings             state.json   load spot, fleet and plants per save
  routes\         recorded roads       dispatcher.log  what the tool did (attach to bug reports)

veilednet - MIT license - https://github.com/veilednet/groundwork-truck-dispatcher
