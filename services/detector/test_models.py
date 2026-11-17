"""Tests for the detection models. Run: python3 test_models.py"""

import random
import sys

from models import (
    CompositeDetector, LoadConditionedBaseline, PersistenceGate,
    SignalSpec, Z_CAP, robust_z,
)

failures = []


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        failures.append(name)


print("robust_z")
check("small population returns 0", robust_z(99.0, [1.0] * 5) == 0.0)

# the bug that shipped: a near-constant signal produced z ~ 2.5e8
z = robust_z(0.25, [0.0] * 40, min_scale=0.05)
check("constant population does not explode", abs(z) <= Z_CAP, f"got {z}")
check("constant population still flags deviation", z > 1.0, f"got {z}")

z_nofloor = robust_z(0.25, [0.0] * 40)
check("capped even with no min_scale", abs(z_nofloor) <= Z_CAP, f"got {z_nofloor}")

normal = [random.gauss(10, 1) for _ in range(300)]
check("normal data: centre scores near 0", abs(robust_z(10.0, normal)) < 0.6)
check("normal data: +4 sigma scores high", robust_z(14.0, normal) > 3.0)
check("outliers do not poison the baseline",
      robust_z(14.0, normal + [500.0] * 8) > 3.0)

print("\nLoadConditionedBaseline")
b = LoadConditionedBaseline()
# latency rises with load, healthily
for _ in range(600):
    load = random.uniform(5, 60)
    b.observe(load, 0.05 + load * 0.004 + random.gauss(0, 0.004))
check("baseline becomes ready", b.ready())
high_load_normal = b.score(58, 0.05 + 58 * 0.004)
check("high load + expected latency = low score", abs(high_load_normal) < 2.0,
      f"got {high_load_normal}")
low_load_bad = b.score(8, 0.05 + 8 * 0.004 + 0.06)
check("low load + high latency = high score", low_load_bad > 3.0,
      f"got {low_load_bad}")

print("\nPersistenceGate")
g = PersistenceGate(enter=3.5, exit_at=2.0, enter_for=3, exit_for=4)
opened_on = [g.update(9.0)[1] for _ in range(3)]
check("does not open on first breach", opened_on[0] is False)
check("opens on the third consecutive breach", opened_on[2] is True)
g2 = PersistenceGate(enter=3.5, exit_at=2.0, enter_for=3, exit_for=4)
spiky = [g2.update(v)[0] for v in (9, 0, 9, 0, 9, 0, 9, 0)]
check("single-sample spikes never open an alert", not any(spiky))

print("\nCompositeDetector.fuse")
specs = {n: SignalSpec(n, "", weight=1.0) for n in ("a", "b", "c")}
lone = CompositeDetector.fuse({"a": 6.0, "b": 0.1, "c": 0.0}, specs)
check("one screaming signal survives fusion", lone >= 6.0, f"got {lone}")
together = CompositeDetector.fuse({"a": 4.0, "b": 4.0, "c": 4.0}, specs)
check("corroborating signals score higher than one alone",
      together > CompositeDetector.fuse({"a": 4.0, "b": 0.0, "c": 0.0}, specs))

print()
if failures:
    print(f"{len(failures)} FAILED: {', '.join(failures)}")
    sys.exit(1)
print("all tests passed")
