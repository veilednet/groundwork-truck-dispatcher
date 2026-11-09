"""Edge cases against fake game memory (no game needed): python dev/edge_tests.py
Each check prints PASS/FAIL; exit code 1 if any failed."""
import json, os, sys
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(HERE, "dev")); sys.path.insert(0, HERE)
import sim_dispatch as S
import dispatcher as D, motion as M, locate
from gwmem import GameGone

failed = []


def check(name, ok, info=""):
    print("%s  %s%s" % ("PASS" if ok else "FAIL", name, ("  (" + info + ")") if info and not ok else ""))
    if not ok:
        failed.append(name)


def fresh(n=2):
    d, pr = S.build(n)
    L, e = S.load_spot_near_plants(d)
    t = d.fleet()[0]
    t.m.put(M.O_POS, L[0], 10.0, L[1], L[2])
    d.learn_load_spot(L[0], L[1], L[2], e)
    t.state, t.last = "AT_LOAD", (L[0], L[1])
    return d, pr, t, L


# 1. game loads another save -> the watchdog stops every write
d, pr, t, L = fresh()
d.check_game()
pr.put_f32(d.fuel0 + locate.OFF_SPAWN, 1.0, 2.0, 3.0)
try:
    d.check_game(); ok = False
except GameGone:
    ok = True
check("watchdog notices another save", ok)
d.disconnect("test")
check("disconnect stops trucks", not d.ready and all(x.follower is None for x in d.trucks))

# 2. bed not found: tips nothing, goes idle (no endless plant <-> plant loop)
d, pr, t, L = fresh()
S.fill(d, t, 25.0)
d.beds = {}
d.send()
S.run(d, 200)
check("no bed: truck goes idle", t.state == "IDLE" and "couldn't tip" in t.note, t.state + " " + t.note)
check("no bed: only one trip", sum(1 for l in d.lines if "%s: to Plant" % t.label in l) == 1)

# 3. the player drives a truck away from the load spot
d, pr, t, L = fresh()
t.m.put(M.O_POS, L[0] + 6, 10.0, L[1], L[2])
S.run(d, 1)
check("driven away from load spot -> MANUAL", t.state == "MANUAL", t.state)

# 4. zero-length route: send again from the tip spot arrives at once and tips
d, pr, t, L = fresh(1)
p = d.plants[0]
x, z, h = p.pose_for(d.dump_offset(t))
t.m.put(M.O_POS, x, 10.0, z, h)
S.fill(d, t, 20.0)
t.state = "IDLE"
d.redo_dump(t.index)
S.run(d, 3)
check("already at the tip spot -> tips", t.state == "DUMPING", t.state)

# 5. state.json: per save, bad indices from another save are dropped
d, pr, t, L = fresh()
d.save_state()
st = json.load(open(D.STATE))
key = d.save.key
st["saves"]["slot-99|other"] = {"load": [1, 2, 3], "rel": {"exc": 999}, "fleet": [999]}
st["saves"][key]["rel"]["exc"] = 999
st["saves"][key]["fleet"] = [t.index, 999]
json.dump(st, open(D.STATE, "w"))
d.load_state()
check("state: bad excavator index dropped", d.rel is None)
check("state: bad fleet index dropped", d.fleet_saved == [t.index], str(d.fleet_saved))
check("state: other saves kept", "slot-99|other" in json.load(open(D.STATE))["saves"])

# 6. config.json with junk
json.dump({"hotkeys": {"menu": "F6", "old_action": "F11", "send": 7}, "speed_loaded_mps": "fast",
           "auto_send": 0, "backin_m": float("nan") if False else 12, "dump_pose": None, "unknown": 1},
          open(D.CONFIG, "w"))
cfg = D.load_config()
check("config: unknown hotkey dropped", "old_action" not in cfg["hotkeys"])
check("config: bad number ignored", cfg["speed_loaded_mps"] == D.DEFAULTS["speed_loaded_mps"])
check("config: bool coerced", cfg["auto_send"] is False)
check("config: unknown key dropped", "unknown" not in cfg and "dump_pose" not in cfg)
open(D.CONFIG, "w").write("{broken")
cfg = D.load_config()
check("config: broken file -> defaults + .broken kept", cfg["backin_m"] == D.DEFAULTS["backin_m"] and
      os.path.exists(D.CONFIG + ".broken"))

# 7. commands: unknown names are refused, nothing crashes
d, pr, t, L = fresh()
open(D.COMMANDS, "w").write("no_such_command 1\nsend\n")
d.poll_command_file()
d._commands()
check("command.txt: unknown command refused", any("Unknown command 'no_such_command'" in l for l in d.lines))

# 8. bed data with a silly length is refused before any write
d, pr, t, L = fresh()
import struct
anchor = d.beds[t.index][0]
pr.write(anchor + d.bed_layout["vec"], struct.pack("<QQQ", 10, 0x40000000, 10 ** 9))
try:
    d.bed_vecs(anchor); ok = False
except RuntimeError:
    ok = True
check("bed with bad length refused", ok)

# 9. pose sanity: NaN never written
d, pr, t, L = fresh()
try:
    t.m.write_drive(float("nan"), 0, 0, 0, 0, 0, 0, 0, 0); ok = False
except ValueError:
    ok = True
check("NaN pose refused", ok)

print("\n%d failed" % len(failed) if failed else "\nall passed")
sys.exit(1 if failed else 0)
