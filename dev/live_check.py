"""Connect to the running game without moving any truck: fleet, plants, planned routes, the camera.
(Connecting itself briefly nudges two unused values to tell the live copy of the data from mirrors.)"""
import os, sys, time
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import numpy as np
import gamecam
gamecam.set_dpi_aware()
import dispatcher as D

d = D.Dispatcher(log=print)
d.connect()
for t in d.trucks:
    x, z, h = t.xz()
    print("  %-10s fleet=%-5s state=%-8s at (%.1f, %.1f) payload %.1f / %.0f  bed %s" % (
        t.label, t.in_fleet, t.state, x, z, t.m.payload(), t.cap, "ok" if t.index in d.beds else "MISSING"))
for p in d.plants:
    print("  %s at (%.1f, %.1f) hopper %.1f m3, addr %s" % (p.name, p.x, p.z, p.hopper(d.proc), hex(p.addr or 0)))
L = d.load_spot()
print("load spot", L)
if not d.ready:
    sys.exit("not connected: " + d.error)
t = (d.fleet() or d.trucks)[0]
x, z, h = t.xz()
for p in d.plants:
    goal = p.pose_for(d.dump_offset(t))
    t0 = time.time()
    plan = d.planner.plan((x, z, h), goal, "backin", d.obstacles(exclude=[t.index]))
    print("plan %s -> %s: %s (%.2f s)" % (t.label, p.name, "none" if plan is None else
                                        "%.0f m, %s" % (plan.length(), plan.note), time.time() - t0))
    if plan:
        path = d.make_path(t, plan, True, "test")
        print("   path %.0f m, %d legs, speeds max %.1f" % (path.length, len(path.leg_end), path.vcap.max()))
if L:
    other = (d.fleet() or d.trucks)[-1]
    ox, oz, oh = other.xz()
    t0 = time.time()
    plan = d.planner.plan((ox, oz, oh), L, "any", d.obstacles(exclude=[other.index]))
    print("plan %s -> load: %s (%.2f s)" % (other.label, "none" if plan is None else
                                          "%.0f m, %s" % (plan.length(), plan.note), time.time() - t0))
hwnd = gamecam.game_window(d.proc.pid)
r = gamecam.client_rect(hwnd)
print("game window", hwnd, r)
tr = gamecam.CameraTracker(d.proc)
cam = None
for _ in range(60):                                   # up to ~6 s: the first scan runs in the background
    cam = tr.update(r[2] / max(1, r[3]))
    if cam is not None:
        break
    time.sleep(0.1)
print("camera:", tr.state)
if cam:
    print("  eye (%.1f, %.1f, %.1f) fov %.0f x %.0f deg" % (*cam.c, 2 * np.degrees(np.arctan(cam.tx)),
                                                         2 * np.degrees(np.arctan(cam.ty))))
    for t in d.trucks[:6]:
        x, y, z, _, _, _ = t.m.pose()
        sx, sy, vis = cam.project([(x, y + 3, z)], r[2], r[3])
        print("  %s on screen at (%.0f, %.0f) %s" % (t.label, sx[0], sy[0], "visible" if vis[0] else "behind"))
